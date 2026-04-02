"""
chronos/core/database.py
========================
Cerveau de Chronos-Git : gestion de l'état via SQLite.

Ce module gère la file d'attente des tâches (task_queue), les profils
de projets et les logs d'exécution. SQLite garantit la persistance même
en cas de coupure de courant ou d'extinction brutale du PC.

Design : Toutes les requêtes passent par des méthodes typées.
         Jamais de SQL brut en dehors de ce module.
"""

import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ── Constantes ──────────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".chronos-git" / "chronos.db"


# ── Énumérations ────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    """États possibles d'une tâche planifiée."""
    PENDING   = "PENDING"    # En attente d'exécution
    RUNNING   = "RUNNING"    # En cours d'exécution
    COMPLETED = "COMPLETED"  # Commit + push réussis
    FAILED    = "FAILED"     # Échec après MAX_RETRY tentatives
    SKIPPED   = "SKIPPED"    # Ignoré manuellement par l'utilisateur


class MergeFrequency(str, Enum):
    """Fréquence de fusion vers la branche principale."""
    FRIDAY      = "friday"
    MONDAY      = "monday"
    EVERY_6DAYS = "6days"
    ON_COMPLETE = "on_complete"  # Quand le dernier fichier est commité
    MANUAL      = "manual"


# ── Dataclasses (modèles) ────────────────────────────────────────────────────

@dataclass
class Task:
    """
    Représente une action planifiée dans la file d'attente.

    Chaque Task correspond à un commit futur d'un fichier vers un dépôt.
    Le champ `file_hash` permet de détecter si le fichier a été modifié
    entre la planification et l'exécution (protection anti-corruption).
    """
    id:               Optional[int]  = None
    project_id:       int            = 0
    file_path:        str            = ""
    commit_message:   str            = ""
    branch_name:      str            = "main"
    scheduled_time:   str            = ""        # ISO 8601 : "2026-04-01 14:30:00"
    status:           TaskStatus     = TaskStatus.PENDING
    retry_count:      int            = 0
    file_hash:        str            = ""        # SHA-256 au moment de la planification
    created_at:       str            = field(default_factory=lambda: datetime.now().isoformat())
    executed_at:      Optional[str]  = None
    error_message:    Optional[str]  = None


@dataclass
class Project:
    """
    Profil d'un projet géré par Chronos-Git.

    Un projet regroupe un ensemble de tâches liées à un même dépôt Git.
    """
    id:              Optional[int] = None
    name:            str           = ""
    repo_path:       str           = ""
    remote_url:      str           = ""
    source_folder:   str           = ""          # Dossier source scanné
    feature_branch:  str           = "main"
    target_branch:   str           = "main"
    merge_frequency: MergeFrequency = MergeFrequency.MANUAL
    total_files:     int           = 0
    completed_files: int           = 0
    created_at:      str           = field(default_factory=lambda: datetime.now().isoformat())
    last_sync_at:    Optional[str] = None


@dataclass
class ExecutionLog:
    """Enregistrement immuable de chaque action exécutée."""
    id:          Optional[int] = None
    project_id:  int           = 0
    task_id:     int           = 0
    action:      str           = ""    # "commit", "push", "merge", "drift_detected"
    detail:      str           = ""
    success:     bool          = True
    timestamp:   str           = field(default_factory=lambda: datetime.now().isoformat())


# ── Gestionnaire de base de données ─────────────────────────────────────────

class Database:
    """
    Interface complète avec la base de données SQLite de Chronos-Git.

    Pattern : Context Manager pour garantir la fermeture des connexions.

    Usage :
        db = Database()
        db.initialize()
        task_id = db.insert_task(task)
    """

    MAX_RETRY = 3  # Nombre maximum de tentatives avant FAILED

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        """
        Ouvre une connexion avec les paramètres optimaux :
        - WAL mode : meilleure concurrence (lecture pendant écriture)
        - Foreign keys activées
        - Row factory pour accès par nom de colonne
        """
        conn = sqlite3.connect(str(self.db_path), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Initialisation ────────────────────────────────────────────────────

    def initialize(self) -> None:
        """
        Crée toutes les tables si elles n'existent pas.
        Appelé à chaque démarrage de l'application (idempotent).
        """
        with self._connect() as conn:
            conn.executescript("""
                -- Table des projets
                CREATE TABLE IF NOT EXISTS projects (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    name             TEXT NOT NULL UNIQUE,
                    repo_path        TEXT NOT NULL,
                    remote_url       TEXT NOT NULL DEFAULT '',
                    source_folder    TEXT NOT NULL DEFAULT '',
                    feature_branch   TEXT NOT NULL DEFAULT 'main',
                    target_branch    TEXT NOT NULL DEFAULT 'main',
                    merge_frequency  TEXT NOT NULL DEFAULT 'manual',
                    total_files      INTEGER NOT NULL DEFAULT 0,
                    completed_files  INTEGER NOT NULL DEFAULT 0,
                    created_at       TEXT NOT NULL,
                    last_sync_at     TEXT
                );

                -- File d'attente des tâches (cœur du système)
                CREATE TABLE IF NOT EXISTS task_queue (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    file_path        TEXT NOT NULL,
                    commit_message   TEXT NOT NULL,
                    branch_name      TEXT NOT NULL DEFAULT 'main',
                    scheduled_time   TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count      INTEGER NOT NULL DEFAULT 0,
                    file_hash        TEXT NOT NULL DEFAULT '',
                    created_at       TEXT NOT NULL,
                    executed_at      TEXT,
                    error_message    TEXT
                );

                -- Index pour accélérer la requête de rattrapage (la plus fréquente)
                CREATE INDEX IF NOT EXISTS idx_tasks_pending
                    ON task_queue(status, scheduled_time)
                    WHERE status = 'PENDING';

                -- Logs d'exécution immuables (append-only)
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id  INTEGER NOT NULL,
                    task_id     INTEGER NOT NULL,
                    action      TEXT NOT NULL,
                    detail      TEXT NOT NULL DEFAULT '',
                    success     INTEGER NOT NULL DEFAULT 1,
                    timestamp   TEXT NOT NULL
                );
            """)

    # ── Projets ───────────────────────────────────────────────────────────

    def insert_project(self, project: Project) -> int:
        """Insère un nouveau projet. Retourne l'ID généré."""
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO projects
                    (name, repo_path, remote_url, source_folder,
                     feature_branch, target_branch, merge_frequency,
                     total_files, completed_files, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                project.name, project.repo_path, project.remote_url,
                project.source_folder, project.feature_branch,
                project.target_branch, project.merge_frequency.value,
                project.total_files, project.completed_files, project.created_at
            ))
            return cursor.lastrowid

    def get_project(self, project_id: int) -> Optional[Project]:
        """Récupère un projet par son ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return self._row_to_project(row) if row else None

    def get_all_projects(self) -> list[Project]:
        """Liste tous les projets enregistrés."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_project(r) for r in rows]

    def update_project_progress(self, project_id: int, completed: int) -> None:
        """Met à jour le compteur de fichiers complétés."""
        with self._connect() as conn:
            conn.execute("""
                UPDATE projects
                SET completed_files = ?, last_sync_at = ?
                WHERE id = ?
            """, (completed, datetime.now().isoformat(), project_id))

    def delete_project(self, project_id: int) -> None:
        """Supprime un projet et toutes ses tâches (CASCADE)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ── Tâches ────────────────────────────────────────────────────────────

    def insert_task(self, task: Task) -> int:
        """Insère une tâche dans la file d'attente. Retourne l'ID."""
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO task_queue
                    (project_id, file_path, commit_message, branch_name,
                     scheduled_time, status, retry_count, file_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.project_id, task.file_path, task.commit_message,
                task.branch_name, task.scheduled_time, task.status.value,
                task.retry_count, task.file_hash, task.created_at
            ))
            return cursor.lastrowid

    def insert_tasks_bulk(self, tasks: list[Task]) -> int:
        """
        Insère plusieurs tâches en une seule transaction.
        Beaucoup plus performant que des insertions individuelles.
        Retourne le nombre de tâches insérées.
        """
        with self._connect() as conn:
            conn.executemany("""
                INSERT INTO task_queue
                    (project_id, file_path, commit_message, branch_name,
                     scheduled_time, status, retry_count, file_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (t.project_id, t.file_path, t.commit_message, t.branch_name,
                 t.scheduled_time, t.status.value, t.retry_count,
                 t.file_hash, t.created_at)
                for t in tasks
            ])
            return len(tasks)

    def get_pending_tasks(self, project_id: Optional[int] = None) -> list[Task]:
        """
        Récupère toutes les tâches PENDING, triées chronologiquement.
        Si project_id est fourni, filtre par projet.
        """
        with self._connect() as conn:
            if project_id:
                rows = conn.execute("""
                    SELECT * FROM task_queue
                    WHERE status = 'PENDING' AND project_id = ?
                    ORDER BY scheduled_time ASC
                """, (project_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM task_queue
                    WHERE status = 'PENDING'
                    ORDER BY scheduled_time ASC
                """).fetchall()
            return [self._row_to_task(r) for r in rows]

    def get_overdue_tasks(self) -> list[Task]:
        """
        Requête critique : récupère les tâches dont l'heure est PASSÉE
        mais qui n'ont pas encore été exécutées.
        C'est le moteur du système de rattrapage.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM task_queue
                WHERE status = 'PENDING' AND scheduled_time <= ?
                ORDER BY scheduled_time ASC
            """, (now,)).fetchall()
            return [self._row_to_task(r) for r in rows]

    def get_upcoming_tasks(self, limit: int = 10) -> list[Task]:
        """Récupère les prochaines tâches futures (pour l'affichage dashboard)."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM task_queue
                WHERE status = 'PENDING' AND scheduled_time > ?
                ORDER BY scheduled_time ASC
                LIMIT ?
            """, (now, limit)).fetchall()
            return [self._row_to_task(r) for r in rows]

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        error_message: Optional[str] = None
    ) -> None:
        """Met à jour le statut d'une tâche après tentative d'exécution."""
        executed_at = datetime.now().isoformat() if status == TaskStatus.COMPLETED else None
        with self._connect() as conn:
            conn.execute("""
                UPDATE task_queue
                SET status = ?, executed_at = ?, error_message = ?
                WHERE id = ?
            """, (status.value, executed_at, error_message, task_id))

    def increment_retry(self, task_id: int) -> int:
        """
        Incrémente le compteur de tentatives et retourne la nouvelle valeur.
        Si retry_count >= MAX_RETRY, la tâche passe en FAILED automatiquement.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE task_queue SET retry_count = retry_count + 1 WHERE id = ?",
                (task_id,)
            )
            row = conn.execute(
                "SELECT retry_count FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            count = row["retry_count"]

            # Passage automatique en FAILED si trop de tentatives
            if count >= self.MAX_RETRY:
                self.update_task_status(
                    task_id,
                    TaskStatus.FAILED,
                    f"Échec après {self.MAX_RETRY} tentatives"
                )
            return count

    def update_task_schedule(self, task_id: int, new_time: str, new_message: str) -> None:
        """Permet à l'utilisateur de reprogrammer une tâche manuellement."""
        with self._connect() as conn:
            conn.execute("""
                UPDATE task_queue
                SET scheduled_time = ?, commit_message = ?
                WHERE id = ?
            """, (new_time, new_message, task_id))

    def skip_task(self, task_id: int) -> None:
        """Marque une tâche comme SKIPPED (choix explicite de l'utilisateur)."""
        self.update_task_status(task_id, TaskStatus.SKIPPED)

    def get_project_stats(self, project_id: int) -> dict:
        """
        Retourne les statistiques complètes d'un projet pour le dashboard.
        """
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status='PENDING'   THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status='FAILED'    THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status='SKIPPED'   THEN 1 ELSE 0 END) as skipped
                FROM task_queue WHERE project_id = ?
            """, (project_id,)).fetchone()
            return dict(row) if row else {}

    # ── Logs ──────────────────────────────────────────────────────────────

    def log_execution(self, log: ExecutionLog) -> None:
        """Enregistre une entrée de log immuable."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO execution_logs
                    (project_id, task_id, action, detail, success, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                log.project_id, log.task_id, log.action,
                log.detail, int(log.success), log.timestamp
            ))

    def get_recent_logs(self, project_id: int, limit: int = 50) -> list[ExecutionLog]:
        """Récupère les derniers logs d'un projet pour l'affichage."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM execution_logs
                WHERE project_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (project_id, limit)).fetchall()
            return [self._row_to_log(r) for r in rows]

    # ── Conversion sqlite3.Row → Dataclass ────────────────────────────────

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            file_path=row["file_path"],
            commit_message=row["commit_message"],
            branch_name=row["branch_name"],
            scheduled_time=row["scheduled_time"],
            status=TaskStatus(row["status"]),
            retry_count=row["retry_count"],
            file_hash=row["file_hash"],
            created_at=row["created_at"],
            executed_at=row["executed_at"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            repo_path=row["repo_path"],
            remote_url=row["remote_url"],
            source_folder=row["source_folder"],
            feature_branch=row["feature_branch"],
            target_branch=row["target_branch"],
            merge_frequency=MergeFrequency(row["merge_frequency"]),
            total_files=row["total_files"],
            completed_files=row["completed_files"],
            created_at=row["created_at"],
            last_sync_at=row["last_sync_at"],
        )

    @staticmethod
    def _row_to_log(row: sqlite3.Row) -> ExecutionLog:
        return ExecutionLog(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            action=row["action"],
            detail=row["detail"],
            success=bool(row["success"]),
            timestamp=row["timestamp"],
        )


# ── Utilitaire de hachage (lié à la DB mais utilisé par le scanner) ──────────

def compute_file_hash(file_path: str) -> str:
    """
    Calcule le hash SHA-256 d'un fichier.

    Utilisé pour détecter si un fichier a été modifié entre
    la planification et l'exécution du commit.

    Lecture par blocs de 8KB pour ne pas charger de gros fichiers en RAM.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            sha256.update(block)
    return sha256.hexdigest()
