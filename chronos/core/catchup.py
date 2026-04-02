"""
chronos/core/catchup.py
=======================
Moteur de rattrapage intelligent (Self-Healing Engine).

C'est le module qui résout le problème central de Chronos-Git :
"Mon PC était éteint pendant 3 jours. Que se passe-t-il ?"

Comportement attendu :
  ❌ Mauvais : Pousser 3 commits d'un coup à l'heure actuelle
  ✅ Bon     : Pousser 3 commits avec les DATES PRÉVUES de chaque jour
               (en ordre chronologique, avec un délai entre chaque)

Ce module est exécuté automatiquement au DÉMARRAGE de l'application.
Il constitue la "conscience temporelle" de Chronos-Git.
"""

import time
from datetime import datetime
from typing import Callable, Optional

from chronos.core.database import Database, Task, ExecutionLog
from chronos.core.executor import GitExecutor, ExecutionAbortedError
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Résultat du rattrapage ───────────────────────────────────────────────────

class CatchupReport:
    """
    Rapport complet d'une session de rattrapage.

    Transmis à l'interface (GUI/CLI) pour afficher le résultat
    à l'utilisateur au démarrage.
    """

    def __init__(self):
        self.total_overdue:   int       = 0
        self.successfully_caught: int   = 0
        self.failed:          int       = 0
        self.skipped:         int       = 0
        self.start_time:      datetime  = datetime.now()
        self.end_time:        Optional[datetime] = None
        self.details:         list[str] = []

    @property
    def duration_seconds(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    @property
    def has_work(self) -> bool:
        return self.total_overdue > 0

    def summary(self) -> str:
        if not self.has_work:
            return "✅ Aucune tâche en retard. Vous êtes à jour !"

        return (
            f"⏱️  Rattrapage terminé en {self.duration_seconds:.1f}s\n"
            f"📋 Tâches en retard    : {self.total_overdue}\n"
            f"✅ Rattrapées avec succès : {self.successfully_caught}\n"
            f"❌ Échouées              : {self.failed}\n"
            f"⏭  Ignorées              : {self.skipped}"
        )


# ── Moteur de rattrapage ─────────────────────────────────────────────────────

class CatchupEngine:
    """
    Exécute toutes les tâches dont la date prévue est passée.

    Principe fondamental :
      Chaque commit rattrapé utilise la date PLANIFIÉE (catchup_mode=True),
      pas la date actuelle. Cela préserve la cohérence du calendrier GitHub.

    Usage :
        engine = CatchupEngine(db, executor)
        report = engine.run()
        print(report.summary())

    Avec callback de progression (pour la GUI) :
        def on_progress(done, total, task):
            update_progress_bar(done / total)

        report = engine.run(progress_callback=on_progress)
    """

    # Délai minimal entre deux commits de rattrapage (en secondes)
    # Évite de saturer l'API GitHub et de déclencher des rate limits
    DELAY_BETWEEN_COMMITS = 2.0

    def __init__(self, db: Database, executor: GitExecutor):
        self.db       = db
        self.executor = executor

    def run(
        self,
        project_id:        Optional[int]                    = None,
        progress_callback: Optional[Callable[[int, int, Task], None]] = None,
        dry_run:           bool                             = False
    ) -> CatchupReport:
        """
        Lance le processus de rattrapage complet.

        Étapes :
        1. Récupère toutes les tâches en retard (date passée + statut PENDING)
        2. Les trie chronologiquement (du plus ancien au plus récent)
        3. Exécute chacune en mode "catchup" (date historique)
        4. Enregistre un rapport complet

        Args:
            project_id        : si fourni, rattrape seulement ce projet
            progress_callback : appelé après chaque tâche (done, total, task)
            dry_run           : si True, simule sans exécuter (mode test)

        Returns:
            CatchupReport avec toutes les statistiques
        """
        report = CatchupReport()

        # Récupère les tâches en retard
        overdue_tasks = self.db.get_overdue_tasks()

        # Filtre par projet si demandé
        if project_id is not None:
            overdue_tasks = [t for t in overdue_tasks if t.project_id == project_id]

        report.total_overdue = len(overdue_tasks)

        if not overdue_tasks:
            logger.info("✅ Aucune tâche en retard détectée.")
            report.end_time = datetime.now()
            return report

        logger.info(
            f"⏱️  {len(overdue_tasks)} tâche(s) en retard détectée(s). "
            f"Démarrage du rattrapage..."
        )

        # Enregistre l'événement en log
        self.db.log_execution(ExecutionLog(
            project_id=project_id or 0,
            task_id=0,
            action="catchup_started",
            detail=f"{len(overdue_tasks)} tâches en retard détectées au démarrage",
            success=True,
        ))

        # ── Exécution chronologique ────────────────────────────────────────
        for i, task in enumerate(overdue_tasks):
            # Vérification du Kill Switch à chaque itération
            if not self.executor.is_active:
                logger.warning("Kill switch actif — Rattrapage interrompu.")
                report.skipped = len(overdue_tasks) - i
                break

            overdue_since = self._format_overdue_duration(task.scheduled_time)
            logger.info(
                f"  [{i+1}/{len(overdue_tasks)}] "
                f"Rattrapage : {task.file_path} "
                f"(prévu il y a {overdue_since})"
            )

            if dry_run:
                # Mode simulation : on log mais on n'exécute pas
                logger.info(f"  [DRY RUN] Simulé : {task.commit_message}")
                report.successfully_caught += 1
                report.details.append(f"✅ [SIM] {task.commit_message}")
            else:
                try:
                    success = self.executor.execute_task(task, catchup_mode=True)
                    if success:
                        report.successfully_caught += 1
                        report.details.append(
                            f"✅ [{task.scheduled_time}] {task.commit_message}"
                        )
                    else:
                        report.failed += 1
                        report.details.append(
                            f"❌ [{task.scheduled_time}] {task.commit_message}"
                        )
                except ExecutionAbortedError:
                    logger.warning("Rattrapage interrompu par Kill Switch.")
                    report.skipped = len(overdue_tasks) - i
                    break
                except Exception as e:
                    logger.exception(f"Erreur inattendue lors du rattrapage : {e}")
                    report.failed += 1

            # Callback de progression pour la GUI (barre de progression, etc.)
            if progress_callback:
                progress_callback(i + 1, len(overdue_tasks), task)

            # Pause entre les commits (respect des limites GitHub)
            if i < len(overdue_tasks) - 1 and not dry_run:
                time.sleep(self.DELAY_BETWEEN_COMMITS)

        report.end_time = datetime.now()

        # Log final du rapport
        self.db.log_execution(ExecutionLog(
            project_id=project_id or 0,
            task_id=0,
            action="catchup_completed",
            detail=report.summary(),
            success=report.failed == 0,
        ))

        logger.info(f"\n{report.summary()}")
        return report

    def check_merge_overdue(
        self,
        repo_path:       str,
        source_branch:   str,
        target_branch:   str,
        merge_frequency: str,
        last_merge_date: Optional[datetime] = None
    ) -> bool:
        """
        Vérifie si un merge périodique aurait dû avoir lieu pendant l'absence.

        Exemple : Si le merge était prévu vendredi et qu'on est lundi,
        détecte le merge manqué et demande confirmation à l'utilisateur.

        Args:
            repo_path       : chemin du dépôt
            source_branch   : branche à fusionner
            target_branch   : branche de destination
            merge_frequency : "friday", "monday", "6days", etc.
            last_merge_date : date du dernier merge effectué

        Returns:
            True si un merge manqué est détecté
        """
        if not last_merge_date:
            return False

        today = datetime.now()
        days_since_merge = (today - last_merge_date).days

        # Logique de détection des merges manqués
        missed = False

        if merge_frequency == "friday":
            # A-t-on raté un vendredi depuis le dernier merge ?
            missed = days_since_merge >= 7
        elif merge_frequency == "monday":
            missed = days_since_merge >= 7
        elif merge_frequency == "6days":
            missed = days_since_merge >= 6

        if missed:
            logger.warning(
                f"⚠️  Merge manqué détecté : {source_branch} → {target_branch}\n"
                f"   Dernier merge : {last_merge_date.strftime('%Y-%m-%d')}\n"
                f"   Jours écoulés : {days_since_merge}"
            )

        return missed

    # ── Méthodes privées ──────────────────────────────────────────────────

    @staticmethod
    def _format_overdue_duration(scheduled_time: str) -> str:
        """
        Formate la durée depuis laquelle une tâche est en retard.
        Ex: "3 jours", "2 heures", "45 minutes"
        """
        try:
            scheduled = datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M:%S")
            delta = datetime.now() - scheduled
            total_seconds = int(delta.total_seconds())

            if total_seconds < 3600:
                minutes = total_seconds // 60
                return f"{minutes} minute(s)"
            elif total_seconds < 86400:
                hours = total_seconds // 3600
                return f"{hours} heure(s)"
            else:
                days = total_seconds // 86400
                return f"{days} jour(s)"
        except Exception:
            return "durée inconnue"
