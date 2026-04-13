"""
chronos/core/task_manager.py
=============================
Gestionnaire complet des tâches planifiées.

Ce module centralise TOUTES les opérations de modification sur les tâches :
  - Édition du message de commit
  - Reprogrammation de la date/heure
  - Annulation individuelle ou en masse
  - Réactivation d'une tâche annulée
  - Réordonnancement (swap de dates entre deux tâches)
  - Édition par lot (changer l'heure de TOUTES les tâches d'une journée)
  - Filtrage avancé (par statut, date, branche, projet)

C'est la pièce manquante entre la Database (stockage brut)
et l'interface utilisateur (GUI / CLI).

Usage :
    mgr = TaskManager(db)

    # Modifier le message d'une tâche
    mgr.edit_message(task_id=5, new_message="fix: corrected login bug")

    # Reprogrammer à une nouvelle heure
    mgr.reschedule(task_id=5, new_datetime="2026-04-15 14:30:00")

    # Annuler une tâche
    mgr.cancel(task_id=5, reason="Fichier supprimé")

    # Annuler toutes les tâches PENDING d'un projet
    mgr.cancel_all_pending(project_id=1)

    # Annulation par plage de dates
    mgr.cancel_range(project_id=1, from_date="2026-04-10", to_date="2026-04-20")

    # Réactiver une tâche annulée
    mgr.reactivate(task_id=5, new_datetime="2026-04-16 10:00:00")

    # Swap de dates entre deux tâches
    mgr.swap_schedule(task_id_a=3, task_id_b=7)

    # Décaler tout un projet d'un nombre de jours
    mgr.shift_all(project_id=1, days_offset=+3)

    # Changer l'heure de toutes les tâches d'une journée
    mgr.set_hour_for_day(project_id=1, day="2026-04-15", hour=16, minute=30)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from chronos.core.database import (
    Database, Task, TaskStatus, compute_file_hash
)
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Résultat d'une opération de masse ────────────────────────────────────────

@dataclass
class BulkOperationResult:
    """Résultat d'une opération appliquée à plusieurs tâches."""
    affected:  int = 0   # Nombre de tâches modifiées
    skipped:   int = 0   # Ignorées (ex: déjà COMPLETED)
    errors:    int = 0   # Erreurs inattendues
    messages:  list = None

    def __post_init__(self):
        if self.messages is None:
            self.messages = []

    @property
    def success(self) -> bool:
        return self.errors == 0

    def summary(self) -> str:
        return (
            f"✅ Modifiées : {self.affected} | "
            f"⏭  Ignorées : {self.skipped} | "
            f"❌ Erreurs : {self.errors}"
        )


# ── Gestionnaire de tâches ────────────────────────────────────────────────────

class TaskManager:
    """
    Interface de haut niveau pour toutes les opérations sur les tâches.

    Règles métier appliquées ici (pas dans la DB) :
      - Une tâche COMPLETED ou RUNNING ne peut pas être éditée
      - Une tâche ne peut pas être reprogrammée dans le passé (sauf force=True)
      - Un message de commit doit respecter le format Conventional Commits
    """

    # Préfixes Conventional Commits acceptés
    VALID_PREFIXES = {
        "feat", "fix", "docs", "style", "refactor",
        "perf", "test", "chore", "build", "ci", "revert",
    }

    def __init__(self, db: Database):
        self.db = db

    # ══════════════════════════════════════════════════════════════════════
    # ── ÉDITION D'UNE SEULE TÂCHE ─────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════

    def edit_message(
        self,
        task_id:     int,
        new_message: str,
        validate:    bool = True,
    ) -> bool:
        """
        Modifie le message de commit d'une tâche.

        Args:
            task_id     : ID de la tâche à modifier
            new_message : nouveau message (ex: "fix: corrected null pointer")
            validate    : si True, vérifie le format Conventional Commits

        Returns:
            True si la modification a réussi
        """
        task = self._get_editable_task(task_id)
        if task is None:
            return False

        new_message = new_message.strip()
        if not new_message:
            logger.error("Le message de commit ne peut pas être vide.")
            return False

        if validate:
            warning = self._validate_commit_message(new_message)
            if warning:
                logger.warning(warning)
                # Avertissement seulement, on n'empêche pas

        self.db.update_task_schedule(task_id, task.scheduled_time, new_message)
        logger.info(
            f"✅ Message modifié : tâche #{task_id}\n"
            f"   Avant : {task.commit_message}\n"
            f"   Après : {new_message}"
        )
        return True

    def reschedule(
        self,
        task_id:      int,
        new_datetime: str,    # Format : "YYYY-MM-DD HH:MM:SS" ou "YYYY-MM-DD HH:MM"
        force:        bool = False,  # Si True, autorise les dates passées
    ) -> bool:
        """
        Reprogramme une tâche à une nouvelle date et heure.

        Args:
            task_id      : ID de la tâche
            new_datetime : nouvelle date/heure ISO 8601
            force        : si True, autorise la reprogrammation dans le passé
                           (utile pour corriger manuellement le calendrier)

        Returns:
            True si la reprogrammation a réussi
        """
        task = self._get_editable_task(task_id)
        if task is None:
            return False

        # Normalise le format (accepte "YYYY-MM-DD HH:MM" sans secondes)
        try:
            dt = self._parse_datetime(new_datetime)
            normalized = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as e:
            logger.error(f"Format de date invalide : {e}")
            return False

        # Avertit si la date est dans le passé
        if dt < datetime.now() and not force:
            logger.warning(
                f"⚠️  La date {normalized} est dans le passé.\n"
                f"   Utilisez force=True pour confirmer (le commit sera immédiat au démarrage)."
            )
            return False

        old_time = task.scheduled_time
        self.db.update_task_schedule(task_id, normalized, task.commit_message)
        logger.info(
            f"✅ Tâche #{task_id} reprogrammée\n"
            f"   Avant : {old_time}\n"
            f"   Après : {normalized}"
        )
        return True

    def edit_task(
        self,
        task_id:      int,
        new_message:  Optional[str] = None,
        new_datetime: Optional[str] = None,
        new_branch:   Optional[str] = None,
        force:        bool = False,
    ) -> bool:
        """
        Édite plusieurs champs d'une tâche en une seule opération.

        Pratique pour la GUI : on passe tous les champs d'un coup,
        seuls ceux qui sont non-None sont modifiés.

        Args:
            task_id      : ID de la tâche
            new_message  : nouveau message (ou None pour ne pas changer)
            new_datetime : nouvelle date (ou None pour ne pas changer)
            new_branch   : nouvelle branche (ou None pour ne pas changer)
            force        : autorise les dates passées

        Returns:
            True si au moins une modification a réussi
        """
        task = self._get_editable_task(task_id)
        if task is None:
            return False

        updated_message  = task.commit_message
        updated_datetime = task.scheduled_time
        modified         = False

        if new_message is not None:
            new_message = new_message.strip()
            if new_message:
                updated_message = new_message
                modified = True

        if new_datetime is not None:
            try:
                dt = self._parse_datetime(new_datetime)
                if dt < datetime.now() and not force:
                    logger.warning(
                        f"⚠️  Date dans le passé ignorée : {new_datetime}\n"
                        f"   Utilisez force=True pour confirmer."
                    )
                else:
                    updated_datetime = dt.strftime("%Y-%m-%d %H:%M:%S")
                    modified = True
            except ValueError as e:
                logger.error(f"Format de date invalide : {e}")

        if new_branch is not None:
            # La branche nécessite une mise à jour directe en DB
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE task_queue SET branch_name = ? WHERE id = ?",
                    (new_branch.strip(), task_id)
                )
            modified = True
            logger.info(f"  Branche changée → {new_branch}")

        if modified:
            self.db.update_task_schedule(task_id, updated_datetime, updated_message)
            logger.info(f"✅ Tâche #{task_id} mise à jour.")

        return modified

    # ══════════════════════════════════════════════════════════════════════
    # ── ANNULATION ────────────────────────────════════════════════════════
    # ══════════════════════════════════════════════════════════════════════

    def cancel(self, task_id: int, reason: str = "") -> bool:
        """
        Annule une tâche individuelle (passe en SKIPPED).

        Une tâche SKIPPED n'est jamais exécutée par le scheduler
        ni par le moteur de rattrapage.

        Args:
            task_id : ID de la tâche à annuler
            reason  : raison de l'annulation (pour les logs)

        Returns:
            True si l'annulation a réussi
        """
        task = self._get_editable_task(task_id)
        if task is None:
            return False

        self.db.skip_task(task_id)

        detail = f"Annulée : {task.commit_message}"
        if reason:
            detail += f" | Raison : {reason}"

        from chronos.core.database import ExecutionLog
        self.db.log_execution(ExecutionLog(
            project_id=task.project_id,
            task_id=task_id,
            action="task_cancelled",
            detail=detail,
            success=True,
        ))

        logger.info(f"✅ Tâche #{task_id} annulée : {task.commit_message}")
        return True

    def cancel_all_pending(
        self,
        project_id: int,
        reason:     str = "Annulation en masse"
    ) -> BulkOperationResult:
        """
        Annule TOUTES les tâches PENDING d'un projet.

        Typiquement utilisé quand un challenge est abandonné ou
        qu'un projet est restructuré.

        Args:
            project_id : ID du projet
            reason     : raison de l'annulation (pour les logs)

        Returns:
            BulkOperationResult avec le nombre de tâches annulées
        """
        result = BulkOperationResult()
        pending_tasks = self.db.get_pending_tasks(project_id)

        if not pending_tasks:
            logger.info(f"Aucune tâche PENDING à annuler pour le projet #{project_id}.")
            return result

        for task in pending_tasks:
            try:
                self.db.skip_task(task.id)
                result.affected += 1
                result.messages.append(f"Annulée : #{task.id} {task.commit_message[:50]}")
            except Exception as e:
                result.errors += 1
                logger.error(f"Erreur annulation tâche #{task.id}: {e}")

        from chronos.core.database import ExecutionLog
        self.db.log_execution(ExecutionLog(
            project_id=project_id,
            task_id=0,
            action="bulk_cancel",
            detail=f"{result.affected} tâches annulées. Raison : {reason}",
            success=result.success,
        ))

        logger.info(f"✅ Annulation en masse : {result.summary()}")
        return result

    def cancel_range(
        self,
        project_id: int,
        from_date:  str,   # "YYYY-MM-DD"
        to_date:    str,   # "YYYY-MM-DD"
        reason:     str = ""
    ) -> BulkOperationResult:
        """
        Annule les tâches PENDING dans une plage de dates.

        Utile pour supprimer les tâches d'une semaine de vacances
        ou d'une période sans activité planifiée.

        Args:
            project_id : ID du projet
            from_date  : date de début incluse ("YYYY-MM-DD")
            to_date    : date de fin incluse ("YYYY-MM-DD")
            reason     : raison (optionnelle)

        Returns:
            BulkOperationResult
        """
        result = BulkOperationResult()

        try:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            dt_to   = datetime.strptime(to_date,   "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
        except ValueError as e:
            logger.error(f"Format de date invalide : {e}")
            result.errors = 1
            return result

        pending = self.db.get_pending_tasks(project_id)

        for task in pending:
            try:
                task_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")
                if dt_from <= task_dt <= dt_to:
                    self.db.skip_task(task.id)
                    result.affected += 1
                    result.messages.append(
                        f"Annulée : #{task.id} [{task.scheduled_time[:10]}] "
                        f"{task.commit_message[:40]}"
                    )
                else:
                    result.skipped += 1
            except Exception as e:
                result.errors += 1
                logger.error(f"Erreur : #{task.id} — {e}")

        logger.info(
            f"✅ Annulation plage [{from_date} → {to_date}] : {result.summary()}"
        )
        return result

    def cancel_by_branch(
        self,
        project_id:  int,
        branch_name: str,
        reason:      str = ""
    ) -> BulkOperationResult:
        """
        Annule toutes les tâches PENDING d'une branche spécifique.

        Utile quand une feature branch est abandonnée.
        """
        result = BulkOperationResult()
        pending = self.db.get_pending_tasks(project_id)

        for task in pending:
            if task.branch_name == branch_name:
                try:
                    self.db.skip_task(task.id)
                    result.affected += 1
                except Exception as e:
                    result.errors += 1
                    logger.error(f"Erreur : #{task.id} — {e}")
            else:
                result.skipped += 1

        logger.info(
            f"✅ Annulation branche '{branch_name}' : {result.summary()}"
        )
        return result

    # ══════════════════════════════════════════════════════════════════════
    # ── RÉACTIVATION ──────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════

    def reactivate(
        self,
        task_id:      int,
        new_datetime: Optional[str] = None
    ) -> bool:
        """
        Réactive une tâche SKIPPED ou FAILED.

        Si new_datetime est fourni, la tâche est reprogrammée à cette date.
        Sinon, elle conserve sa date originale (qui sera dans le passé,
        donc rattrapée immédiatement au prochain démarrage).

        Args:
            task_id      : ID de la tâche à réactiver
            new_datetime : nouvelle date (optionnelle)

        Returns:
            True si la réactivation a réussi
        """
        # Récupère la tâche (accepte SKIPPED et FAILED)
        task = self._get_task_any_status(task_id)
        if task is None:
            return False

        if task.status == TaskStatus.COMPLETED:
            logger.error(
                f"Tâche #{task_id} déjà COMPLETED. "
                "Impossible de la réactiver (le commit existe déjà sur GitHub)."
            )
            return False

        if task.status == TaskStatus.PENDING:
            logger.warning(f"Tâche #{task_id} est déjà PENDING. Aucune action nécessaire.")
            return True

        # Remet en PENDING
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE task_queue SET status = 'PENDING', retry_count = 0, error_message = NULL WHERE id = ?",
                (task_id,)
            )

        # Reprogramme si une nouvelle date est fournie
        if new_datetime:
            try:
                dt = self._parse_datetime(new_datetime)
                normalized = dt.strftime("%Y-%m-%d %H:%M:%S")
                self.db.update_task_schedule(task_id, normalized, task.commit_message)
                logger.info(
                    f"✅ Tâche #{task_id} réactivée → {normalized}"
                )
            except ValueError as e:
                logger.error(f"Format de date invalide : {e}")
        else:
            logger.info(
                f"✅ Tâche #{task_id} réactivée (date originale : {task.scheduled_time})"
            )

        return True

    def reactivate_all_skipped(
        self,
        project_id:   int,
        shift_days:   int = 0  # Décaler les dates réactivées de N jours
    ) -> BulkOperationResult:
        """
        Réactive toutes les tâches SKIPPED d'un projet.

        Utile après une annulation par erreur ou pour reprendre
        un projet après une pause.

        Args:
            project_id : ID du projet
            shift_days : décalage optionnel des dates (+N jours)
        """
        result = BulkOperationResult()

        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_queue WHERE project_id = ? AND status = 'SKIPPED'",
                (project_id,)
            ).fetchall()

        for row in rows:
            try:
                # Calcule la nouvelle date si décalage demandé
                if shift_days != 0:
                    orig_dt = datetime.strptime(row["scheduled_time"], "%Y-%m-%d %H:%M:%S")
                    new_dt  = orig_dt + timedelta(days=shift_days)
                    new_time = new_dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    new_time = row["scheduled_time"]

                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE task_queue SET status='PENDING', retry_count=0, error_message=NULL, scheduled_time=? WHERE id=?",
                        (new_time, row["id"])
                    )
                result.affected += 1
            except Exception as e:
                result.errors += 1
                logger.error(f"Réactivation #{row['id']} : {e}")

        logger.info(f"✅ Réactivation masse : {result.summary()}")
        return result

    # ══════════════════════════════════════════════════════════════════════
    # ── OPÉRATIONS DE PLANIFICATION EN MASSE ──────────────────────────────
    # ══════════════════════════════════════════════════════════════════════

    def shift_all(
        self,
        project_id:  int,
        days_offset: int,
        hours_offset: int = 0,
        from_date:   Optional[str] = None,   # Si fourni, décale seulement à partir de cette date
    ) -> BulkOperationResult:
        """
        Décale TOUTES les tâches PENDING d'un projet de N jours (et/ou heures).

        Exemple : le challenge prend du retard de 3 jours → shift_all(+3)
        Exemple : tout décaler d'une semaine → shift_all(+7)
        Exemple : tout rapprocher de 2 jours → shift_all(-2)

        Args:
            project_id   : ID du projet
            days_offset  : nombre de jours à ajouter (négatif pour reculer)
            hours_offset : nombre d'heures à ajouter (optionnel)
            from_date    : si fourni, ne décale que les tâches après cette date

        Returns:
            BulkOperationResult
        """
        result  = BulkOperationResult()
        pending = self.db.get_pending_tasks(project_id)

        filter_dt = None
        if from_date:
            try:
                filter_dt = datetime.strptime(from_date, "%Y-%m-%d")
            except ValueError:
                logger.error(f"Format from_date invalide : {from_date}")
                result.errors = 1
                return result

        for task in pending:
            try:
                task_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")

                # Filtre si from_date est spécifié
                if filter_dt and task_dt < filter_dt:
                    result.skipped += 1
                    continue

                new_dt = task_dt + timedelta(days=days_offset, hours=hours_offset)
                new_time = new_dt.strftime("%Y-%m-%d %H:%M:%S")

                self.db.update_task_schedule(task.id, new_time, task.commit_message)
                result.affected += 1
                result.messages.append(
                    f"#{task.id} : {task_dt.strftime('%m-%d %H:%M')} → {new_dt.strftime('%m-%d %H:%M')}"
                )
            except Exception as e:
                result.errors += 1
                logger.error(f"Shift tâche #{task.id} : {e}")

        direction = "+" if days_offset >= 0 else ""
        logger.info(
            f"✅ Décalage {direction}{days_offset}j {'+' if hours_offset >= 0 else ''}{hours_offset}h : {result.summary()}"
        )
        return result

    def set_hour_for_day(
        self,
        project_id: int,
        day:        str,    # "YYYY-MM-DD"
        hour:       int,    # 0-23
        minute:     int = 0,
    ) -> BulkOperationResult:
        """
        Change l'heure de TOUTES les tâches d'un jour donné.

        Exemple : "Je veux que tous les commits du 15 avril soient à 22h30"
        → set_hour_for_day(1, "2026-04-15", 22, 30)

        Args:
            project_id : ID du projet
            day        : date au format "YYYY-MM-DD"
            hour       : heure cible (0-23)
            minute     : minute cible (0-59)

        Returns:
            BulkOperationResult
        """
        result = BulkOperationResult()

        if not (0 <= hour <= 23):
            logger.error(f"Heure invalide : {hour} (doit être entre 0 et 23)")
            result.errors = 1
            return result

        pending = self.db.get_pending_tasks(project_id)

        for task in pending:
            task_day = task.scheduled_time[:10]
            if task_day == day:
                try:
                    task_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")
                    new_dt  = task_dt.replace(hour=hour, minute=minute, second=0)
                    self.db.update_task_schedule(
                        task.id,
                        new_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        task.commit_message
                    )
                    result.affected += 1
                    result.messages.append(f"#{task.id} → {new_dt.strftime('%H:%M')}")
                except Exception as e:
                    result.errors += 1
                    logger.error(f"set_hour tâche #{task.id} : {e}")
            else:
                result.skipped += 1

        logger.info(
            f"✅ Heure {hour:02d}:{minute:02d} pour le {day} : {result.summary()}"
        )
        return result

    def set_daily_push_time(
        self,
        project_id: int,
        hour:       int,
        minute:     int = 0,
        jitter_min: int = 0,  # Variation aléatoire en minutes (0 = heure exacte)
        from_date:  Optional[str] = None,
    ) -> BulkOperationResult:
        """
        Définit une heure de push quotidienne uniforme pour toutes les tâches.

        Exemple : "Je veux pousser tous les jours à 18h30"
        → set_daily_push_time(1, hour=18, minute=30)

        Avec jitter pour paraître naturel :
        → set_daily_push_time(1, hour=18, minute=30, jitter_min=20)
        → Chaque tâche sera entre 18h10 et 18h50

        Args:
            project_id : ID du projet
            hour       : heure cible (0-23)
            minute     : minute cible (0-59)
            jitter_min : variation aléatoire ±N minutes
            from_date  : appliquer seulement à partir de cette date

        Returns:
            BulkOperationResult
        """
        import random
        result  = BulkOperationResult()
        pending = self.db.get_pending_tasks(project_id)

        filter_dt = None
        if from_date:
            try:
                filter_dt = datetime.strptime(from_date, "%Y-%m-%d")
            except ValueError:
                result.errors = 1
                return result

        for task in pending:
            try:
                task_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")

                if filter_dt and task_dt < filter_dt:
                    result.skipped += 1
                    continue

                # Applique l'heure avec jitter optionnel
                jitter = random.randint(-jitter_min, jitter_min) if jitter_min > 0 else 0
                new_dt = task_dt.replace(hour=hour, minute=minute, second=0)
                new_dt += timedelta(minutes=jitter)

                # Garde l'heure dans [0, 23]
                if new_dt.day != task_dt.day:
                    new_dt = task_dt.replace(hour=hour, minute=minute, second=0)

                self.db.update_task_schedule(
                    task.id,
                    new_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    task.commit_message
                )
                result.affected += 1
            except Exception as e:
                result.errors += 1
                logger.error(f"set_daily_push_time #{task.id} : {e}")

        logger.info(
            f"✅ Heure quotidienne {hour:02d}:{minute:02d} "
            f"(jitter ±{jitter_min}min) : {result.summary()}"
        )
        return result

    def swap_schedule(self, task_id_a: int, task_id_b: int) -> bool:
        """
        Échange les dates planifiées de deux tâches.

        Utile pour réordonner manuellement des fichiers dans le planning
        sans avoir à tout reprogrammer manuellement.

        Args:
            task_id_a : ID de la première tâche
            task_id_b : ID de la deuxième tâche

        Returns:
            True si le swap a réussi
        """
        task_a = self._get_editable_task(task_id_a)
        task_b = self._get_editable_task(task_id_b)

        if task_a is None or task_b is None:
            return False

        # Échange les dates
        self.db.update_task_schedule(task_id_a, task_b.scheduled_time, task_a.commit_message)
        self.db.update_task_schedule(task_id_b, task_a.scheduled_time, task_b.commit_message)

        logger.info(
            f"✅ Swap effectué :\n"
            f"   #{task_id_a} ({Path(task_a.file_path).name}) : "
            f"{task_a.scheduled_time} → {task_b.scheduled_time}\n"
            f"   #{task_id_b} ({Path(task_b.file_path).name}) : "
            f"{task_b.scheduled_time} → {task_a.scheduled_time}"
        )
        return True

    def set_push_days(
        self,
        project_id:   int,
        allowed_days: list[int],   # 0=Lundi, 1=Mardi, ..., 6=Dimanche
        hour:         int = 14,
        minute:       int = 0,
        jitter_min:   int = 30,
    ) -> BulkOperationResult:
        """
        Redistribue les tâches pour qu'elles tombent UNIQUEMENT sur certains jours.

        Exemple : "Je veux pousser seulement le lundi, mercredi et vendredi"
        → set_push_days(1, allowed_days=[0, 2, 4], hour=16)

        Exemple : "Jamais le week-end"
        → set_push_days(1, allowed_days=[0,1,2,3,4], hour=18)

        L'algorithme redistribue les tâches sur les prochains jours autorisés
        en maintenant l'ordre chronologique original.

        Args:
            project_id   : ID du projet
            allowed_days : liste des jours autorisés (0=Lun, 6=Dim)
            hour         : heure de push sur ces jours
            minute       : minute de push
            jitter_min   : variation aléatoire ±N minutes

        Returns:
            BulkOperationResult
        """
        import random
        result  = BulkOperationResult()
        pending = self.db.get_pending_tasks(project_id)

        if not pending:
            return result

        if not allowed_days:
            logger.error("Aucun jour autorisé spécifié.")
            result.errors = 1
            return result

        # Déduplique et trie les jours autorisés
        allowed_days = sorted(set(d % 7 for d in allowed_days))

        # Trouve la première date future autorisée
        start_dt = datetime.now()
        while start_dt.weekday() not in allowed_days:
            start_dt += timedelta(days=1)

        # Redistribue les tâches sur les jours autorisés
        current_dt = start_dt.replace(hour=hour, minute=minute, second=0)
        last_assigned_day = None

        for task in pending:
            try:
                # Passe au prochain jour autorisé si on a déjà assigné aujourd'hui
                if last_assigned_day == current_dt.date():
                    # Passe au prochain jour autorisé
                    current_dt += timedelta(days=1)
                    while current_dt.weekday() not in allowed_days:
                        current_dt += timedelta(days=1)
                    current_dt = current_dt.replace(hour=hour, minute=minute, second=0)

                # Applique le jitter
                jitter = random.randint(-jitter_min, jitter_min) if jitter_min > 0 else 0
                final_dt = current_dt + timedelta(minutes=jitter)
                if final_dt.date() != current_dt.date():
                    final_dt = current_dt  # Ne sort pas du jour

                self.db.update_task_schedule(
                    task.id,
                    final_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    task.commit_message
                )
                last_assigned_day = current_dt.date()
                result.affected += 1

            except Exception as e:
                result.errors += 1
                logger.error(f"set_push_days #{task.id} : {e}")

        day_names = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        days_str = ", ".join(day_names[d] for d in allowed_days)
        logger.info(
            f"✅ Jours de push : [{days_str}] à {hour:02d}:{minute:02d} "
            f"(jitter ±{jitter_min}min) : {result.summary()}"
        )
        return result

    def bulk_edit_messages(
        self,
        project_id: int,
        prefix:     str,         # "feat", "fix", "docs"...
        from_date:  Optional[str] = None,
        to_date:    Optional[str] = None,
    ) -> BulkOperationResult:
        """
        Change le préfixe de commit de toutes les tâches (ou d'une plage).

        Utile pour uniformiser le style des commits d'un challenge.
        Exemple : changer tous les "feat:" en "docs:" pour un challenge de documentation.

        Args:
            project_id : ID du projet
            prefix     : nouveau préfixe ("feat", "fix", "docs"...)
            from_date  : date de début (optionnelle)
            to_date    : date de fin (optionnelle)

        Returns:
            BulkOperationResult
        """
        result = BulkOperationResult()

        if prefix not in self.VALID_PREFIXES:
            logger.warning(
                f"Préfixe '{prefix}' non standard. "
                f"Préfixes valides : {', '.join(sorted(self.VALID_PREFIXES))}"
            )

        pending = self.db.get_pending_tasks(project_id)

        # Filtre par plage de dates si spécifié
        dt_from = datetime.strptime(from_date, "%Y-%m-%d") if from_date else None
        dt_to   = datetime.strptime(to_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        ) if to_date else None

        for task in pending:
            try:
                task_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")

                if dt_from and task_dt < dt_from:
                    result.skipped += 1
                    continue
                if dt_to and task_dt > dt_to:
                    result.skipped += 1
                    continue

                # Remplace le préfixe actuel ou ajoute le nouveau
                old_msg = task.commit_message
                if ":" in old_msg:
                    # Remplace le préfixe existant
                    suffix   = old_msg.split(":", 1)[1].strip()
                    new_msg  = f"{prefix}: {suffix}"
                else:
                    new_msg  = f"{prefix}: {old_msg}"

                self.db.update_task_schedule(task.id, task.scheduled_time, new_msg)
                result.affected += 1
                result.messages.append(f"#{task.id} : '{old_msg}' → '{new_msg}'")

            except Exception as e:
                result.errors += 1
                logger.error(f"bulk_edit_messages #{task.id} : {e}")

        logger.info(f"✅ Préfixes → '{prefix}' : {result.summary()}")
        return result

    # ══════════════════════════════════════════════════════════════════════
    # ── FILTRAGE ET REQUÊTES AVANCÉES ─────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════

    def get_tasks_for_day(self, project_id: int, day: str) -> list[Task]:
        """
        Récupère toutes les tâches planifiées pour un jour donné.

        Args:
            project_id : ID du projet
            day        : "YYYY-MM-DD"

        Returns:
            Liste de Task pour ce jour
        """
        pending = self.db.get_pending_tasks(project_id)
        return [t for t in pending if t.scheduled_time.startswith(day)]

    def get_tasks_by_status(
        self,
        project_id: int,
        status:     TaskStatus
    ) -> list[Task]:
        """Filtre les tâches par statut."""
        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_queue WHERE project_id = ? AND status = ? ORDER BY scheduled_time ASC",
                (project_id, status.value)
            ).fetchall()
            return [self.db._row_to_task(r) for r in rows]

    def get_all_tasks(
        self,
        project_id:   int,
        include_done: bool = False,
    ) -> list[Task]:
        """
        Récupère toutes les tâches d'un projet (tous statuts).

        Args:
            project_id   : ID du projet
            include_done : si False, exclut COMPLETED et SKIPPED

        Returns:
            Liste de tâches triées par date planifiée
        """
        with self.db._connect() as conn:
            if include_done:
                rows = conn.execute(
                    "SELECT * FROM task_queue WHERE project_id = ? ORDER BY scheduled_time ASC",
                    (project_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM task_queue
                       WHERE project_id = ? AND status IN ('PENDING', 'FAILED', 'RUNNING')
                       ORDER BY scheduled_time ASC""",
                    (project_id,)
                ).fetchall()
            return [self.db._row_to_task(r) for r in rows]

    def search_tasks(
        self,
        project_id: int,
        keyword:    str,
    ) -> list[Task]:
        """
        Recherche des tâches par mot-clé dans le message ou le chemin de fichier.

        Args:
            project_id : ID du projet
            keyword    : mot-clé de recherche (insensible à la casse)

        Returns:
            Liste de tâches correspondantes
        """
        keyword_lower = keyword.lower()
        all_tasks = self.get_all_tasks(project_id, include_done=True)
        return [
            t for t in all_tasks
            if keyword_lower in t.commit_message.lower()
            or keyword_lower in t.file_path.lower()
        ]

    def get_calendar_summary(self, project_id: int) -> dict[str, list[Task]]:
        """
        Retourne un dictionnaire date → liste de tâches.
        Utile pour afficher un calendrier dans la GUI.

        Returns:
            {"2026-04-01": [Task, Task], "2026-04-02": [Task], ...}
        """
        all_tasks = self.get_all_tasks(project_id, include_done=False)
        calendar: dict[str, list[Task]] = {}

        for task in all_tasks:
            day = task.scheduled_time[:10]
            if day not in calendar:
                calendar[day] = []
            calendar[day].append(task)

        return calendar

    # ══════════════════════════════════════════════════════════════════════
    # ── MÉTHODES PRIVÉES ──────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════

    def _get_editable_task(self, task_id: int) -> Optional[Task]:
        """
        Récupère une tâche et vérifie qu'elle est éditable (PENDING ou FAILED).
        Retourne None avec un log d'erreur si la tâche ne peut pas être modifiée.
        """
        task = self._get_task_any_status(task_id)
        if task is None:
            return None

        if task.status in (TaskStatus.COMPLETED, TaskStatus.RUNNING):
            logger.error(
                f"Tâche #{task_id} en statut {task.status.value}. "
                "Impossible de la modifier après exécution."
            )
            return None

        return task

    def _get_task_any_status(self, task_id: int) -> Optional[Task]:
        """Récupère une tâche par son ID, quel que soit son statut."""
        with self.db._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()

        if row is None:
            logger.error(f"Tâche #{task_id} introuvable.")
            return None

        return self.db._row_to_task(row)

    @staticmethod
    def _parse_datetime(dt_str: str) -> datetime:
        """
        Parse une chaîne datetime en acceptant plusieurs formats.

        Formats acceptés :
          - "2026-04-15 14:30:00"  (complet)
          - "2026-04-15 14:30"     (sans secondes)
          - "2026-04-15"           (date seule → heure = 09:00:00)
        """
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(dt_str.strip(), fmt)
                # Si seulement la date, on met 9h par défaut
                if fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=9, minute=0, second=0)
                return dt
            except ValueError:
                continue
        raise ValueError(
            f"Format de date non reconnu : '{dt_str}'. "
            "Utilisez 'YYYY-MM-DD HH:MM' ou 'YYYY-MM-DD HH:MM:SS'."
        )

    @staticmethod
    def _validate_commit_message(message: str) -> Optional[str]:
        """
        Valide le format Conventional Commits.
        Retourne un message d'avertissement ou None si valide.
        """
        if ":" not in message:
            return (
                f"Message '{message}' ne suit pas le format Conventional Commits.\n"
                "Exemples : 'feat: ma fonctionnalité', 'fix: bug corrigé', 'docs: README'"
            )
        prefix = message.split(":")[0].strip().lower()
        # Accepte aussi les scopes : "feat(auth): ..."
        if "(" in prefix:
            prefix = prefix.split("(")[0]
        valid = {
            "feat", "fix", "docs", "style", "refactor",
            "perf", "test", "chore", "build", "ci", "revert",
        }
        if prefix not in valid:
            return (
                f"Préfixe '{prefix}' non standard. "
                f"Préfixes Conventional Commits : {', '.join(sorted(valid))}"
            )
        return None