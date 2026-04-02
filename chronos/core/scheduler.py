"""
chronos/core/scheduler.py
==========================
Planificateur en arrière-plan basé sur APScheduler.

Ce module fait tourner un daemon Python qui surveille la file d'attente
et exécute les commits à l'heure prévue — pendant que l'application
tourne en arrière-plan (mode "homme de l'ombre").

Deux jobs périodiques :
  1. check_due_tasks   → toutes les 5 minutes
     Vérifie si des tâches sont dues à l'instant. Si oui, les exécute.

  2. check_merge_cycle → toutes les heures
     Vérifie si un merge périodique doit avoir lieu (vendredi, 6 jours...)

  3. check_drift       → tous les jours à 08h00
     Analyse silencieuse des fichiers locaux modifiés ou nouveaux.
     Notifie l'utilisateur si une dérive est détectée.

Usage :
    scheduler = ChronosScheduler(db, executor, catchup)
    scheduler.start()
    ...
    scheduler.stop()
"""

import threading
from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from chronos.core.database import Database, Task, MergeFrequency
from chronos.core.executor import GitExecutor, ExecutionAbortedError
from chronos.core.catchup import CatchupEngine
from chronos.core.scanner import FolderScanner
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


class ChronosScheduler:
    """
    Daemon de planification en arrière-plan pour Chronos-Git.

    Lance APScheduler en mode "background" : un thread séparé qui
    surveille la file d'attente pendant que l'UI reste réactive.

    Callbacks disponibles (pour notifier l'interface) :
      on_task_completed(task)      : un commit a réussi
      on_task_failed(task, error)  : un commit a échoué
      on_drift_detected(files)     : des fichiers locaux ont changé
      on_merge_needed(project)     : un merge périodique est dû
    """

    # Intervalle de vérification des tâches dues (en minutes)
    CHECK_INTERVAL_MINUTES = 5

    def __init__(
        self,
        db:       Database,
        executor: GitExecutor,
        catchup:  CatchupEngine,
        scanner:  Optional[FolderScanner] = None,
    ):
        self.db       = db
        self.executor = executor
        self.catchup  = catchup
        self.scanner  = scanner or FolderScanner(db)

        # Callbacks optionnels (branchés par la GUI)
        self.on_task_completed:  Optional[Callable[[Task], None]]        = None
        self.on_task_failed:     Optional[Callable[[Task, str], None]]   = None
        self.on_drift_detected:  Optional[Callable[[list[str]], None]]   = None
        self.on_merge_needed:    Optional[Callable[[object], None]]      = None

        # APScheduler en mode "background" (thread daemon)
        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce":   True,   # Si plusieurs exécutions sont en retard, n'en fait qu'une
                "max_instances": 1,   # Pas de jobs parallèles du même type
            }
        )
        self._running = False
        self._lock    = threading.Lock()  # Protection contre les race conditions

    # ── Cycle de vie ──────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Démarre le scheduler en arrière-plan.

        Ajoute les 3 jobs périodiques et démarre le thread daemon.
        Idempotent : appeler start() deux fois n'est pas dangereux.
        """
        if self._running:
            logger.warning("Le scheduler est déjà actif.")
            return

        # ── Job 1 : Vérification des tâches dues (toutes les 5 min) ──────
        self._scheduler.add_job(
            func=self._check_due_tasks,
            trigger=IntervalTrigger(minutes=self.CHECK_INTERVAL_MINUTES),
            id="check_due_tasks",
            name="Vérification des tâches dues",
            replace_existing=True,
        )

        # ── Job 2 : Vérification des merges (toutes les heures) ──────────
        self._scheduler.add_job(
            func=self._check_merge_cycles,
            trigger=IntervalTrigger(hours=1),
            id="check_merge_cycles",
            name="Vérification des cycles de merge",
            replace_existing=True,
        )

        # ── Job 3 : Drift Detection (tous les jours à 08h00) ─────────────
        self._scheduler.add_job(
            func=self._daily_drift_check,
            trigger=CronTrigger(hour=8, minute=0),
            id="daily_drift_check",
            name="Analyse quotidienne de dérive",
            replace_existing=True,
        )

        self._scheduler.start()
        self._running = True
        logger.info(
            f"⏱️  Chronos Scheduler démarré.\n"
            f"   Vérification des tâches : toutes les {self.CHECK_INTERVAL_MINUTES} min\n"
            f"   Merges périodiques      : toutes les heures\n"
            f"   Drift detection         : tous les jours à 08h00"
        )

    def stop(self) -> None:
        """Arrête proprement le scheduler (attend la fin des jobs en cours)."""
        if not self._running:
            return

        self._scheduler.shutdown(wait=True)
        self._running = False
        logger.info("⏹️  Chronos Scheduler arrêté.")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_next_job_times(self) -> dict[str, Optional[datetime]]:
        """
        Retourne les prochaines heures d'exécution de chaque job.
        Utile pour l'affichage dans le dashboard.
        """
        result = {}
        for job in self._scheduler.get_jobs():
            next_run = job.next_run_time
            result[job.id] = next_run
        return result

    # ── Jobs périodiques ──────────────────────────────────────────────────

    def _check_due_tasks(self) -> None:
        """
        Job 1 : Exécute les tâches dont l'heure planifiée est atteinte.

        Appelé toutes les CHECK_INTERVAL_MINUTES minutes.
        Utilise un lock pour éviter les exécutions parallèles.
        """
        # Utilise un lock non-bloquant : si déjà en cours, on passe
        if not self._lock.acquire(blocking=False):
            logger.debug("Job check_due_tasks déjà en cours, skip.")
            return

        try:
            overdue_tasks = self.db.get_overdue_tasks()

            if not overdue_tasks:
                logger.debug("check_due_tasks : aucune tâche due.")
                return

            logger.info(f"⏰ {len(overdue_tasks)} tâche(s) à exécuter maintenant.")

            for task in overdue_tasks:
                # Arrêt si Kill Switch activé
                if not self.executor.is_active:
                    logger.warning("Kill Switch actif — Scheduler suspendu.")
                    break

                try:
                    # catchup_mode=True : utilise la date planifiée
                    # (même si le commit est exécuté quelques minutes en retard)
                    success = self.executor.execute_task(task, catchup_mode=True)

                    if success:
                        logger.info(f"  ✅ Exécuté : {task.commit_message}")
                        if self.on_task_completed:
                            self.on_task_completed(task)
                    else:
                        logger.warning(f"  ⚠️  Échec récupérable : {task.commit_message}")
                        if self.on_task_failed:
                            self.on_task_failed(task, "Échec lors de l'exécution")

                except ExecutionAbortedError:
                    logger.warning("Scheduler : Kill Switch détecté en cours de job.")
                    break
                except Exception as e:
                    logger.exception(f"  ❌ Erreur inattendue : {e}")
                    if self.on_task_failed:
                        self.on_task_failed(task, str(e))

        finally:
            self._lock.release()

    def _check_merge_cycles(self) -> None:
        """
        Job 2 : Vérifie si des merges périodiques doivent avoir lieu.

        Pour chaque projet avec une fréquence de merge configurée,
        teste si aujourd'hui est un jour de merge.
        """
        projects = self.db.get_all_projects()

        for project in projects:
            if project.merge_frequency == MergeFrequency.MANUAL:
                continue  # Ignore les projets en mode manuel

            # Vérifie si aujourd'hui est un jour de merge
            should_merge = self.executor.should_merge_today(
                project.merge_frequency.value,
                last_merge_date=None,  # TODO: stocker la dernière date de merge en DB
            )

            if should_merge:
                logger.info(
                    f"📅 Merge périodique déclenché : "
                    f"{project.feature_branch} → {project.target_branch}"
                )

                if self.on_merge_needed:
                    self.on_merge_needed(project)
                else:
                    # Exécute directement si pas de callback (CLI mode)
                    self.executor.merge_branch(
                        repo_path=project.repo_path,
                        source_branch=project.feature_branch,
                        target_branch=project.target_branch,
                    )

    def _daily_drift_check(self) -> None:
        """
        Job 3 : Analyse quotidienne silencieuse de la dérive des fichiers.

        Exécutée chaque matin à 08h00. Si des fichiers modifiés ou nouveaux
        sont détectés, notifie l'utilisateur via le callback on_drift_detected.
        """
        projects = self.db.get_all_projects()

        for project in projects:
            if not project.source_folder:
                continue

            result = self.scanner.analyze_drift(project.source_folder, project.id)

            if result.total_actionable > 0:
                logger.warning(
                    f"🔍 Dérive détectée dans '{project.name}' :\n"
                    f"{result.summary()}"
                )

                actionable_files = result.new_files + result.modified_files

                if self.on_drift_detected:
                    self.on_drift_detected(actionable_files)
