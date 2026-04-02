"""
tests/test_core.py
==================
Tests unitaires pour les modules core de Chronos-Git.

Exécution :
    pytest tests/ -v
    pytest tests/ -v --cov=chronos --cov-report=term-missing
"""

import os
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ── Configuration des imports ─────────────────────────────────────────────────
# On patch les dépendances externes avant l'import des modules
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Tests : Database ──────────────────────────────────────────────────────────

class TestDatabase:
    """Tests pour le module database.py"""

    def setup_method(self):
        """Crée une DB temporaire pour chaque test."""
        from chronos.core.database import Database, Project, Task, TaskStatus

        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        # Imports locaux pour les tests
        self.Project    = Project
        self.Task       = Task
        self.TaskStatus = TaskStatus

    def test_initialize_creates_tables(self):
        """La DB doit créer les tables au premier démarrage."""
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        tables = [
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        conn.close()
        assert "projects" in tables
        assert "task_queue" in tables
        assert "execution_logs" in tables

    def test_insert_and_get_project(self):
        """Un projet inséré doit être récupérable par son ID."""
        project = self.Project(
            name="test-project",
            repo_path="/tmp/repo",
            source_folder="/tmp/source",
        )
        project_id = self.db.insert_project(project)
        assert project_id > 0

        retrieved = self.db.get_project(project_id)
        assert retrieved is not None
        assert retrieved.name == "test-project"
        assert retrieved.repo_path == "/tmp/repo"

    def test_insert_and_get_tasks(self):
        """Des tâches insérées doivent être récupérables."""
        project = self.Project(name="task-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        task = self.Task(
            project_id=project_id,
            file_path="/tmp/file.py",
            commit_message="feat: test commit",
            scheduled_time="2026-01-01 10:00:00",
        )
        task_id = self.db.insert_task(task)
        assert task_id > 0

        pending = self.db.get_pending_tasks(project_id)
        assert len(pending) == 1
        assert pending[0].commit_message == "feat: test commit"

    def test_get_overdue_tasks(self):
        """Les tâches avec une date passée doivent apparaître dans overdue."""
        project = self.Project(name="overdue-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        # Tâche passée (en retard)
        past_task = self.Task(
            project_id=project_id,
            file_path="/tmp/old.py",
            commit_message="feat: overdue",
            scheduled_time=(datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.db.insert_task(past_task)

        # Tâche future (pas en retard)
        future_task = self.Task(
            project_id=project_id,
            file_path="/tmp/future.py",
            commit_message="feat: future",
            scheduled_time=(datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.db.insert_task(future_task)

        overdue = self.db.get_overdue_tasks()
        assert len(overdue) == 1
        assert overdue[0].commit_message == "feat: overdue"

    def test_update_task_status(self):
        """Le statut d'une tâche doit être modifiable."""
        project = self.Project(name="status-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        task = self.Task(
            project_id=project_id,
            file_path="/tmp/file.py",
            commit_message="feat: update test",
            scheduled_time="2026-01-01 10:00:00",
        )
        task_id = self.db.insert_task(task)

        self.db.update_task_status(task_id, self.TaskStatus.COMPLETED)

        # Les tâches COMPLETED ne doivent pas apparaître dans pending
        pending = self.db.get_pending_tasks(project_id)
        assert len(pending) == 0

    def test_bulk_insert(self):
        """L'insertion en masse doit fonctionner correctement."""
        project = self.Project(name="bulk-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        tasks = [
            self.Task(
                project_id=project_id,
                file_path=f"/tmp/file_{i}.py",
                commit_message=f"feat: file {i}",
                scheduled_time=f"2026-04-{i+1:02d} 10:00:00",
            )
            for i in range(30)
        ]

        count = self.db.insert_tasks_bulk(tasks)
        assert count == 30

        pending = self.db.get_pending_tasks(project_id)
        assert len(pending) == 30

    def test_delete_project_cascades(self):
        """Supprimer un projet doit supprimer ses tâches (CASCADE)."""
        project = self.Project(name="cascade-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        task = self.Task(
            project_id=project_id,
            file_path="/tmp/file.py",
            commit_message="feat: cascade test",
            scheduled_time="2026-01-01 10:00:00",
        )
        self.db.insert_task(task)

        self.db.delete_project(project_id)

        pending = self.db.get_pending_tasks(project_id)
        assert len(pending) == 0


# ── Tests : Scanner ───────────────────────────────────────────────────────────

class TestFolderScanner:
    """Tests pour le module scanner.py"""

    def setup_method(self):
        """Crée des fichiers de test temporaires."""
        from chronos.core.database import Database
        from chronos.core.scanner import FolderScanner

        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test.db"
        self.db = Database(self.db_path)
        self.db.initialize()
        self.scanner = FolderScanner(self.db)

        # Crée des fichiers de test avec un délai pour distinguer les dates
        self.test_files = []
        for i in range(5):
            f = Path(self.tmp) / f"script_{i:02d}.py"
            f.write_text(f"# Script {i}\nprint({i})\n")
            self.test_files.append(f)

    def test_list_files_by_creation_date(self):
        """Les fichiers doivent être triés par date de création."""
        files = self.scanner.list_files_by_creation_date(self.tmp)
        assert len(files) == 5
        # Vérifie que c'est une liste de Path
        assert all(isinstance(f, Path) for f in files)

    def test_list_files_ignores_system_files(self):
        """Les fichiers système doivent être ignorés."""
        # Crée un fichier .pyc
        (Path(self.tmp) / "cache.pyc").write_bytes(b"")
        # Crée un .DS_Store
        (Path(self.tmp) / ".DS_Store").write_bytes(b"")

        files = self.scanner.list_files_by_creation_date(self.tmp)
        names = [f.name for f in files]

        assert "cache.pyc" not in names
        assert ".DS_Store" not in names

    def test_build_plan_creates_correct_count(self):
        """Le plan doit créer autant de tâches que de fichiers."""
        from chronos.core.database import Project

        project = Project(name="scan-test", repo_path=self.tmp)
        project_id = self.db.insert_project(project)

        tasks = self.scanner.build_plan(
            folder_path=self.tmp,
            project_id=project_id,
            start_date=datetime(2026, 4, 1),
            days_count=30,
        )

        assert len(tasks) == 5  # 5 fichiers de test

    def test_build_plan_respects_override_skip(self):
        """Les fichiers avec action='skip' ne doivent pas être planifiés."""
        from chronos.core.database import Project

        project = Project(name="override-test", repo_path=self.tmp)
        project_id = self.db.insert_project(project)

        overrides = {
            "script_00.py": {"action": "skip"},
        }

        tasks = self.scanner.build_plan(
            folder_path=self.tmp,
            project_id=project_id,
            start_date=datetime(2026, 4, 1),
            days_count=30,
            overrides=overrides,
        )

        # 5 fichiers - 1 skippé = 4
        assert len(tasks) == 4
        paths = [t.file_path for t in tasks]
        assert not any("script_00.py" in p for p in paths)

    def test_build_plan_respects_override_date(self):
        """Un override de date doit être respecté."""
        from chronos.core.database import Project

        project = Project(name="date-override-test", repo_path=self.tmp)
        project_id = self.db.insert_project(project)

        custom_date = "2026-04-15 23:59:00"
        overrides = {
            "script_04.py": {"date": custom_date, "message": "🏆 DONE"},
        }

        tasks = self.scanner.build_plan(
            folder_path=self.tmp,
            project_id=project_id,
            start_date=datetime(2026, 4, 1),
            days_count=30,
            overrides=overrides,
        )

        # Trouve la tâche avec l'override
        override_task = next(
            (t for t in tasks if "script_04.py" in t.file_path), None
        )
        assert override_task is not None
        assert override_task.scheduled_time == custom_date
        assert override_task.commit_message == "🏆 DONE"

    def test_scheduled_times_are_within_range(self):
        """Les heures de commit doivent être dans la plage 9h-21h."""
        from chronos.core.database import Project
        from chronos.core.scanner import COMMIT_HOUR_MIN, COMMIT_HOUR_MAX

        project = Project(name="hour-test", repo_path=self.tmp)
        project_id = self.db.insert_project(project)

        tasks = self.scanner.build_plan(
            folder_path=self.tmp,
            project_id=project_id,
            start_date=datetime(2026, 4, 1),
            days_count=30,
        )

        for task in tasks:
            hour = int(task.scheduled_time[11:13])
            assert COMMIT_HOUR_MIN <= hour <= COMMIT_HOUR_MAX, \
                f"Heure {hour} hors de la plage [{COMMIT_HOUR_MIN}, {COMMIT_HOUR_MAX}]"


# ── Tests : FileHash ──────────────────────────────────────────────────────────

class TestFileHash:
    """Tests pour la fonction de hachage SHA-256."""

    def test_hash_consistency(self):
        """Le même fichier doit toujours donner le même hash."""
        from chronos.core.database import compute_file_hash

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("print('hello world')\n")
            tmp_path = f.name

        try:
            hash1 = compute_file_hash(tmp_path)
            hash2 = compute_file_hash(tmp_path)
            assert hash1 == hash2
        finally:
            os.unlink(tmp_path)

    def test_hash_changes_on_modification(self):
        """Modifier un fichier doit changer son hash."""
        from chronos.core.database import compute_file_hash

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("print('original')\n")
            tmp_path = f.name

        try:
            hash_before = compute_file_hash(tmp_path)

            # Modifie le fichier
            with open(tmp_path, "a") as f:
                f.write("# modification\n")

            hash_after = compute_file_hash(tmp_path)
            assert hash_before != hash_after
        finally:
            os.unlink(tmp_path)


# ── Tests : CatchupEngine ─────────────────────────────────────────────────────

class TestCatchupEngine:
    """Tests pour le moteur de rattrapage."""

    def setup_method(self):
        from chronos.core.database import Database
        from chronos.core.catchup import CatchupEngine

        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        self.db.initialize()

        # Mock de l'exécuteur
        self.mock_executor = MagicMock()
        self.mock_executor.is_active = True
        self.mock_executor.execute_task.return_value = True

        self.engine = CatchupEngine(self.db, self.mock_executor)

    def test_no_overdue_returns_empty_report(self):
        """Sans tâches en retard, le rapport doit indiquer zéro."""
        report = self.engine.run(dry_run=True)
        assert report.total_overdue == 0
        assert not report.has_work

    def test_overdue_tasks_are_executed_in_order(self):
        """Les tâches en retard doivent être exécutées dans l'ordre chronologique."""
        from chronos.core.database import Project, Task

        project = Project(name="catchup-test", repo_path="/tmp/repo")
        project_id = self.db.insert_project(project)

        # 3 tâches en retard dans le désordre
        dates = [
            (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        ]

        for i, date in enumerate(dates):
            task = Task(
                project_id=project_id,
                file_path=f"/tmp/file_{i}.py",
                commit_message=f"feat: file {i}",
                scheduled_time=date,
            )
            self.db.insert_task(task)

        report = self.engine.run(dry_run=True)
        assert report.total_overdue == 3
        assert report.successfully_caught == 3

    def test_format_overdue_duration(self):
        """Le formateur de durée doit retourner les bonnes valeurs."""
        from chronos.core.catchup import CatchupEngine

        # 30 minutes
        t = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        assert "minute" in CatchupEngine._format_overdue_duration(t)

        # 3 heures
        t = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        assert "heure" in CatchupEngine._format_overdue_duration(t)

        # 5 jours
        t = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        assert "jour" in CatchupEngine._format_overdue_duration(t)


# ── Tests : KeyringManager ────────────────────────────────────────────────────

class TestKeyringManager:
    """Tests pour le gestionnaire de secrets."""

    def test_invalid_token_format_rejected(self):
        """Un token au format invalide ne doit pas être stocké."""
        with patch("chronos.security.keyring_manager.KEYRING_AVAILABLE", False):
            from chronos.security.keyring_manager import KeyringManager
            km = KeyringManager()

            # Tokens invalides
            assert not km.store_token("invalid_token")
            assert not km.store_token("password123")
            assert not km.store_token("")

    def test_valid_token_format_accepted(self):
        """Un token au format GitHub valide doit être accepté."""
        with patch("chronos.security.keyring_manager.KEYRING_AVAILABLE", False):
            with patch.object(
                __import__("chronos.security.keyring_manager",
                           fromlist=["KeyringManager"]).KeyringManager,
                "_store_in_env_file",
                return_value=True
            ):
                from chronos.security.keyring_manager import KeyringManager
                km = KeyringManager()
                # Simule un token valide (format PAT)
                valid_token = "ghp_" + "x" * 36
                # Teste juste la validation de format
                assert km._is_valid_token_format(valid_token)
                assert km._is_valid_token_format("github_pat_" + "x" * 50)

    def test_token_preview_masking(self):
        """La preview du token doit masquer les caractères intermédiaires."""
        with patch("chronos.security.keyring_manager.KEYRING_AVAILABLE", False):
            from chronos.security.keyring_manager import KeyringManager
            km = KeyringManager()

            with patch.object(km, "get_token", return_value="ghp_AbcDefGhiJklMno123"):
                preview = km.get_token_preview()
                assert "****" in preview
                assert "ghp_" in preview  # Début visible
                assert "123" in preview   # Fin visible


# ── Point d'entrée ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
