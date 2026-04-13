"""
chronos/core/executor.py
========================
Moteur d'exécution Git de Chronos-Git.

Ce module est la "main droite" de l'orchestrateur : il effectue
les opérations Git réelles (checkout, add, commit, push, merge, PR)
en utilisant GitPython et l'API GitHub.

Principe de sécurité central :
  - Avant chaque commit, le hash du fichier est REVÉRIFIÉ.
  - Si le hash a changé depuis la planification, l'exécution est BLOQUÉE.
  - L'utilisateur est notifié et peut décider de la marche à suivre.

Particularité du "rattrapage historique" :
  - Les commits passés utilisent GIT_COMMITTER_DATE + GIT_AUTHOR_DATE
  - Cela "remplit" le calendrier GitHub à la date PRÉVUE, pas actuelle.
"""

import os
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

import git
from git import Repo, GitCommandError

from chronos.core.database import (
    Database, Task, ExecutionLog, TaskStatus, compute_file_hash
)
from chronos.security.keyring_manager import KeyringManager
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Exceptions personnalisées ────────────────────────────────────────────────

class HashMismatchError(Exception):
    """Levée quand le hash d'un fichier a changé depuis la planification."""
    pass


class GitAuthError(Exception):
    """Levée quand l'authentification GitHub échoue."""
    pass


class ExecutionAbortedError(Exception):
    """Levée quand l'exécution est stoppée par le Kill Switch."""
    pass


# ── Exécuteur principal ──────────────────────────────────────────────────────

class GitExecutor:
    """
    Exécute les opérations Git planifiées dans la file d'attente.

    Supporte deux modes d'exécution :
    - Normal   : commit à la date actuelle (tâches en temps réel)
    - Catchup  : commit à la date PLANIFIÉE (rattrapage historique)

    Usage :
        executor = GitExecutor(db, keyring)
        executor.execute_task(task, catchup_mode=False)
        executor.merge_branch("feat/challenge", "main", repo_path)
    """

    def __init__(self, db: Database, keyring: KeyringManager):
        self.db      = db
        self.keyring = keyring
        self._kill_switch = False  # Si True, toute exécution est bloquée

    # ── Kill Switch ───────────────────────────────────────────────────────

    def activate_kill_switch(self) -> None:
        """Arrête IMMÉDIATEMENT toute exécution en cours ou future."""
        self._kill_switch = True
        logger.warning("🔴 KILL SWITCH ACTIVÉ — Toutes les tâches sont suspendues.")

    def deactivate_kill_switch(self) -> None:
        """Réactive l'exécuteur après vérification manuelle."""
        self._kill_switch = False
        logger.info("🟢 Kill Switch désactivé — Reprise des opérations.")

    @property
    def is_active(self) -> bool:
        return not self._kill_switch

    # ── Exécution d'une tâche ─────────────────────────────────────────────

    def execute_task(self, task: Task, catchup_mode: bool = False) -> bool:
        """
        Exécute un commit pour une tâche planifiée.

        Étapes :
        1. Vérifie le Kill Switch
        2. Vérifie l'intégrité du fichier (hash SHA-256)
        3. Récupère le token depuis le Keyring (jamais en dur)
        4. Se positionne sur la bonne branche
        5. Ajoute et committe le fichier
        6. Pousse vers le remote
        7. Met à jour la base de données

        Args:
            task         : la tâche à exécuter
            catchup_mode : si True, utilise la date planifiée (pas aujourd'hui)

        Returns:
            True si succès, False si échec récupérable
        """
        # ── Garde 1 : Kill Switch ─────────────────────────────────────────
        if self._kill_switch:
            raise ExecutionAbortedError("Kill switch actif. Opération annulée.")

        # ── Garde 2 : Vérification de l'intégrité du fichier ─────────────
        self._verify_file_integrity(task)

        # ── Garde 3 : Le fichier existe encore ? ─────────────────────────
        file_path = Path(task.file_path)
        if not file_path.exists():
            error = f"Fichier introuvable : {task.file_path}"
            logger.error(error)
            self._mark_failed(task, error)
            return False

        # ── Opération Git ─────────────────────────────────────────────────
        try:
            repo = self._get_repo(task)

            # Passe sur la bonne branche (crée si inexistante)
            self._checkout_or_create_branch(repo, task.branch_name)

            # Ajoute le fichier à l'index
            repo.index.add([str(file_path)])
            logger.debug(f"  git add {file_path.name}")

            # Détermine la date du commit
            commit_date = self._resolve_commit_date(task, catchup_mode)

            # Crée le commit avec la date (historique ou actuelle)
            self._commit_with_date(repo, task.commit_message, commit_date)
            logger.info(f"  ✅ Commit : [{commit_date}] {task.commit_message}")

            # Pousse vers le remote
            self._push(repo, task.branch_name)
            logger.info(f"  🚀 Push réussi → {task.branch_name}")

            # Mise à jour de la base de données
            self.db.update_task_status(task.id, TaskStatus.COMPLETED)

            # Enregistrement du log
            self.db.log_execution(ExecutionLog(
                project_id=task.project_id,
                task_id=task.id,
                action="commit+push",
                detail=f"[{commit_date}] {task.commit_message} → {task.branch_name}",
                success=True,
            ))

            return True

        except HashMismatchError as e:
            logger.error(f"  🔒 Hash mismatch bloqué : {e}")
            self._mark_failed(task, str(e))
            return False

        except GitCommandError as e:
            # Extrait le message lisible depuis l'exception GitPython
            git_msg = str(e).replace("\n", " ").strip()
            logger.error(f"  ❌ Erreur Git : {git_msg[:300]}")
            # Sauvegarde l'erreur dans la DB AVANT increment_retry
            try:
                self.db.update_task_status(task.id, TaskStatus.FAILED, git_msg[:500])
            except Exception:
                pass
            try:
                retry = self.db.increment_retry(task.id)
                logger.warning(f"  Tentative {retry}/{Database.MAX_RETRY}")
            except Exception as db_err:
                logger.error(f"  DB increment_retry échoué : {db_err}")
            return False

        except Exception as e:
            logger.exception(f"  ❌ Erreur inattendue : {e}")
            try:
                self._mark_failed(task, str(e)[:500])
            except Exception as db_err:
                logger.error(f"  DB _mark_failed échoué : {db_err}")
            return False

    # ── Gestion des branches ──────────────────────────────────────────────

    def checkout_or_create_branch(
        self,
        repo_path:   str,
        branch_name: str,
        from_branch: str = "main"
    ) -> bool:
        """
        Crée une nouvelle branche feature ou bascule sur une existante.

        Args:
            repo_path   : chemin du dépôt local
            branch_name : nom de la branche cible (ex: "feat/security-challenge")
            from_branch : branche de base si création (défaut: "main")

        Returns:
            True si succès
        """
        try:
            repo = Repo(repo_path)
            return self._checkout_or_create_branch(repo, branch_name, from_branch)
        except Exception as e:
            logger.error(f"Impossible de gérer la branche {branch_name}: {e}")
            return False

    def merge_branch(
        self,
        repo_path:     str,
        source_branch: str,
        target_branch: str = "main",
        commit_date:   Optional[str] = None
    ) -> bool:
        """
        Fusionne source_branch dans target_branch.

        Utilisé pour les merges périodiques (vendredi, tous les 6 jours, etc.)
        Optionnellement, la date du merge peut être forcée pour le rattrapage.

        Args:
            repo_path     : chemin du dépôt
            source_branch : branche à fusionner (ex: "feat/security-challenge")
            target_branch : branche de destination (ex: "main")
            commit_date   : date forcée pour le merge commit (rattrapage)

        Returns:
            True si le merge s'est effectué sans conflit
        """
        try:
            repo = Repo(repo_path)

            logger.info(f"  Merge : {source_branch} → {target_branch}")
            repo.git.checkout(target_branch)
            repo.git.merge(source_branch, no_ff=True)

            # Push du merge vers le remote
            repo.remotes.origin.push()
            logger.info(f"  ✅ Merge réussi + push → {target_branch}")

            return True

        except GitCommandError as e:
            if "CONFLICT" in str(e):
                logger.error(f"  ⚠️  Conflit de merge détecté. Intervention manuelle requise.")
            else:
                logger.error(f"  ❌ Erreur de merge : {e}")
            return False

    def should_merge_today(
        self,
        merge_frequency: str,
        last_merge_date: Optional[datetime] = None
    ) -> bool:
        """
        Détermine si aujourd'hui est un jour de merge selon la fréquence configurée.

        Fréquences supportées :
          - "friday"      : chaque vendredi (weekday = 4)
          - "monday"      : chaque lundi (weekday = 0)
          - "6days"       : tous les 6 jours depuis le dernier merge
          - "on_complete" : géré ailleurs (quand le dernier fichier est commité)
          - "manual"      : jamais automatique

        Args:
            merge_frequency : identifiant de la fréquence
            last_merge_date : date du dernier merge (pour le calcul d'intervalle)

        Returns:
            True si un merge doit être déclenché aujourd'hui
        """
        today = datetime.now()
        weekday = today.weekday()  # 0=Lundi, 4=Vendredi, 6=Dimanche

        if merge_frequency == "friday":
            return weekday == 4

        if merge_frequency == "monday":
            return weekday == 0

        if merge_frequency == "6days" and last_merge_date:
            delta = (today - last_merge_date).days
            return delta >= 6

        return False

    # ── Méthodes privées ──────────────────────────────────────────────────

    def _get_repo(self, task: Task) -> Repo:
        """
        Récupère l'objet Repo GitPython pour la tâche.
        Remonte depuis le chemin du fichier pour trouver le .git.
        """
        file_path = Path(task.file_path)
        # Cherche le .git en remontant l'arborescence
        for parent in [file_path.parent] + list(file_path.parents):
            if (parent / ".git").exists():
                return Repo(str(parent))
        raise FileNotFoundError(f"Aucun dépôt Git trouvé pour : {task.file_path}")

    def _checkout_or_create_branch(
        self,
        repo: Repo,
        branch_name: str,
        from_branch: str = "main"
    ) -> bool:
        """Bascule sur une branche ou la crée si elle n'existe pas."""
        existing = [b.name for b in repo.branches]

        if branch_name in existing:
            repo.git.checkout(branch_name)
            logger.debug(f"  Basculé sur la branche existante : {branch_name}")
        else:
            # Crée depuis la branche de base
            if from_branch in existing:
                repo.git.checkout(from_branch)
            repo.git.checkout("-b", branch_name)
            logger.info(f"  Nouvelle branche créée : {branch_name}")

        return True

    def _resolve_commit_date(self, task: Task, catchup_mode: bool) -> str:
        """
        Détermine la date du commit.

        En mode rattrapage : retourne la date PLANIFIÉE de la tâche.
        En mode normal    : retourne l'heure ACTUELLE.

        Cette distinction est cruciale : le mode rattrapage "remplit"
        les cases vides du calendrier GitHub avec les bonnes dates.
        """
        if catchup_mode:
            return task.scheduled_time  # La date prévue, pas aujourd'hui
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _commit_with_date(self, repo: Repo, message: str, commit_date: str) -> None:
        """
        Crée un commit Git en forçant la date (auteur + committer).

        Utilise les variables d'environnement GIT_AUTHOR_DATE et
        GIT_COMMITTER_DATE pour que GitHub affiche la bonne date
        dans le calendrier des contributions.

        Cas particulier "nothing to commit" :
          Si git status --porcelain est vide, le fichier est déjà commité
          ou identique à HEAD. On ne lève PAS d'exception — c'est un succès
          silencieux (le fichier était déjà là, on marque COMPLETED quand même).
        """
        # Convertit depuis notre format DB vers le format ISO 8601 de Git
        try:
            dt = datetime.strptime(commit_date, "%Y-%m-%d %H:%M:%S")
            iso_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            iso_date = commit_date  # Déjà au bon format

        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"]    = iso_date
        env["GIT_COMMITTER_DATE"] = iso_date

        # Vérifie d'abord que quelque chose est stagé
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.working_dir,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            # Rien à committer — fichier déjà identique à HEAD ou non modifié
            # Ce n'est PAS une erreur : on considère que c'est déjà fait
            logger.info(f"  ℹ️  Rien à committer pour ce fichier (déjà dans l'index).")
            return  # Pas d'exception → execute_task marquera COMPLETED

        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo.working_dir,
            env=env,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Loggue le vrai message Git (stdout + stderr) pour le diagnostic
            git_output = (result.stdout + result.stderr).strip()
            logger.error(f"  git commit stdout/stderr : {git_output[:500]}")
            raise GitCommandError("git commit", result.returncode, git_output)

    def _push(self, repo: Repo, branch_name: str) -> None:
        """
        Pousse la branche vers le remote 'origin'.

        Utilise le token GitHub stocké dans le Keyring pour l'authentification
        HTTPS. Jamais de credentials en clair dans le code ou les logs.
        """
        try:
            # Tente d'abord un push simple (si SSH ou credential helper configuré)
            repo.remotes.origin.push(branch_name)
        except GitCommandError as e:
            # Fallback : injection du token HTTPS depuis le Keyring
            token = self.keyring.get_token()
            if token:
                remote_url = list(repo.remotes.origin.urls)[0]
                # Construit l'URL avec le token intégré (jamais loggé)
                if remote_url.startswith("https://github.com/"):
                    auth_url = remote_url.replace(
                        "https://github.com/",
                        f"https://{token}@github.com/"
                    )
                    repo.remotes.origin.set_url(auth_url)
                    repo.remotes.origin.push(branch_name)
                    # Restaure l'URL sans token après le push
                    repo.remotes.origin.set_url(remote_url)
            else:
                raise GitAuthError(
                    "Token GitHub introuvable. "
                    "Exécutez : python main.py security setup-token"
                ) from e

    def _verify_file_integrity(self, task: Task) -> None:
        """
        Vérifie que le fichier n'a pas été modifié depuis la planification.

        Si le hash a changé, l'exécution est BLOQUÉE et l'utilisateur
        est notifié. C'est le garde-fou anti-corruption et anti-malware.

        Ne bloque PAS si le hash de planification est vide (fichier sans hash).
        """
        if not task.file_hash:
            return  # Pas de hash enregistré, on ne peut pas vérifier

        try:
            current_hash = compute_file_hash(task.file_path)
        except FileNotFoundError:
            return  # Le fichier n'existe plus, géré ailleurs

        if current_hash != task.file_hash:
            raise HashMismatchError(
                f"Le fichier '{Path(task.file_path).name}' a été modifié "
                f"depuis sa planification.\n"
                f"Hash prévu  : {task.file_hash[:16]}...\n"
                f"Hash actuel : {current_hash[:16]}...\n"
                f"Action requise : Replanifier ou ignorer ce fichier."
            )

    def _mark_failed(self, task: Task, error: str) -> None:
        """Marque une tâche comme FAILED et enregistre l'erreur."""
        self.db.update_task_status(task.id, TaskStatus.FAILED, error)
        self.db.log_execution(ExecutionLog(
            project_id=task.project_id,
            task_id=task.id,
            action="commit_failed",
            detail=error,
            success=False,
        ))