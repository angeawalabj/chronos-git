"""
tests/test_task_manager.py
===========================
Tests unitaires complets pour le module TaskManager.

Couvre toutes les opérations :
  - Édition individuelle (message, date, branche)
  - Annulation individuelle et en masse
  - Annulation par plage de dates et par branche
  - Réactivation (individuelle et en masse)
  - Décalage (shift_all)
  - Heure quotidienne (set_daily_push_time)
  - Jours de push (set_push_days)
  - Swap de dates
  - Recherche et filtres
  - Validation des messages Conventional Commits
"""

import sys
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from chronos.core.database import Database, Project, Task, TaskStatus
from chronos.core.task_manager import TaskManager, BulkOperationResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_db() -> Database:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "test.db")
    db.initialize()
    return db


def make_project(db: Database, name: str = "test-project") -> int:
    p = Project(name=name, repo_path="/tmp/repo")
    return db.insert_project(p)


def make_task(
    db:       Database,
    pid:      int,
    filename: str       = "script.py",
    message:  str       = "feat: test",
    days_from_now: int  = 1,
    branch:   str       = "main",
    status:   TaskStatus = TaskStatus.PENDING,
) -> int:
    dt = (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d %H:%M:%S")
    t  = Task(
        project_id=pid,
        file_path=f"/tmp/{filename}",
        commit_message=message,
        branch_name=branch,
        scheduled_time=dt,
        status=status,
    )
    tid = db.insert_task(t)
    # Force le statut si ce n'est pas PENDING (la DB insère toujours PENDING)
    if status != TaskStatus.PENDING:
        db.update_task_status(tid, status)
    return tid


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉDITION INDIVIDUELLE ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestEditMessage:

    def test_edit_message_success(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, message="feat: old message")
        mgr = TaskManager(db)

        result = mgr.edit_message(tid, "fix: new message")
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.commit_message == "fix: new message"

    def test_edit_message_empty_rejected(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        result = mgr.edit_message(tid, "")
        assert result is False

    def test_edit_completed_task_rejected(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, status=TaskStatus.COMPLETED)
        mgr = TaskManager(db)

        result = mgr.edit_message(tid, "fix: should fail")
        assert result is False

        # Le message ne doit pas avoir changé
        task = mgr._get_task_any_status(tid)
        assert task.commit_message == "feat: test"

    def test_edit_nonexistent_task(self):
        db  = make_db()
        mgr = TaskManager(db)
        result = mgr.edit_message(99999, "fix: ghost")
        assert result is False


class TestReschedule:

    def test_reschedule_future_date(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        future = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        result = mgr.reschedule(tid, future)
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.scheduled_time == future

    def test_reschedule_past_date_blocked_without_force(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        past = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        result = mgr.reschedule(tid, past, force=False)
        assert result is False

    def test_reschedule_past_date_allowed_with_force(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        past = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        result = mgr.reschedule(tid, past, force=True)
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.scheduled_time == past

    def test_reschedule_accepts_short_format(self):
        """Accepte 'YYYY-MM-DD HH:MM' sans secondes."""
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        result = mgr.reschedule(tid, "2027-01-15 14:30")
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.scheduled_time == "2027-01-15 14:30:00"

    def test_reschedule_accepts_date_only(self):
        """Accepte 'YYYY-MM-DD' → heure par défaut 09:00."""
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        result = mgr.reschedule(tid, "2027-06-01")
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.scheduled_time == "2027-06-01 09:00:00"


class TestEditTask:

    def test_edit_multiple_fields_at_once(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, message="feat: old", branch="main")
        mgr = TaskManager(db)

        result = mgr.edit_task(
            tid,
            new_message="docs: updated readme",
            new_datetime="2027-05-01 10:00:00",
            new_branch="feat/docs-update",
        )
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.commit_message == "docs: updated readme"
        assert task.scheduled_time == "2027-05-01 10:00:00"
        assert task.branch_name == "feat/docs-update"

    def test_edit_none_fields_unchanged(self):
        """Si un champ est None, il doit rester inchangé."""
        db  = make_db()
        pid = make_project(db)
        original_dt = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        tid = make_task(db, pid, message="feat: keep this", days_from_now=5)
        mgr = TaskManager(db)

        result = mgr.edit_task(tid, new_message="fix: changed only message")
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.commit_message == "fix: changed only message"
        # La date ne doit pas avoir changé
        assert task.scheduled_time[:16] == original_dt[:16]


# ══════════════════════════════════════════════════════════════════════════════
# ── ANNULATION ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestCancel:

    def test_cancel_pending_task(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid)
        mgr = TaskManager(db)

        result = mgr.cancel(tid)
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.status == TaskStatus.SKIPPED

    def test_cancel_completed_task_rejected(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, status=TaskStatus.COMPLETED)
        mgr = TaskManager(db)

        result = mgr.cancel(tid)
        assert result is False

    def test_cancel_all_pending(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        # 5 tâches PENDING
        for i in range(5):
            make_task(db, pid, filename=f"f{i}.py", days_from_now=i+1)

        result = mgr.cancel_all_pending(pid)
        assert result.affected == 5
        assert result.errors == 0

        # Vérifie qu'elles sont toutes SKIPPED
        pending = db.get_pending_tasks(pid)
        assert len(pending) == 0

    def test_cancel_all_does_not_affect_completed(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        make_task(db, pid, filename="done.py",    status=TaskStatus.COMPLETED)
        make_task(db, pid, filename="pending.py", status=TaskStatus.PENDING, days_from_now=1)

        result = mgr.cancel_all_pending(pid)
        assert result.affected == 1  # Seulement le PENDING

    def test_cancel_range_date_filtering(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        # Tâche DANS la plage
        t_in  = Task(project_id=pid, file_path="/tmp/in.py",
                     commit_message="feat: in range",
                     scheduled_time="2026-04-15 10:00:00")
        db.insert_task(t_in)

        # Tâche HORS plage
        t_out = Task(project_id=pid, file_path="/tmp/out.py",
                     commit_message="feat: out of range",
                     scheduled_time="2026-04-25 10:00:00")
        db.insert_task(t_out)

        result = mgr.cancel_range(pid, "2026-04-10", "2026-04-20")
        assert result.affected == 1
        assert result.skipped == 1

    def test_cancel_by_branch(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        make_task(db, pid, filename="feat.py",   branch="feat/ui",      days_from_now=1)
        make_task(db, pid, filename="main.py",   branch="main",         days_from_now=2)
        make_task(db, pid, filename="other.py",  branch="feat/ui",      days_from_now=3)

        result = mgr.cancel_by_branch(pid, "feat/ui")
        assert result.affected == 2
        assert result.skipped  == 1


# ══════════════════════════════════════════════════════════════════════════════
# ── RÉACTIVATION ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestReactivate:

    def test_reactivate_skipped_task(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, status=TaskStatus.SKIPPED)
        mgr = TaskManager(db)

        result = mgr.reactivate(tid)
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.status == TaskStatus.PENDING

    def test_reactivate_with_new_date(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, status=TaskStatus.SKIPPED)
        mgr = TaskManager(db)

        new_dt = "2027-12-01 09:00:00"
        result = mgr.reactivate(tid, new_datetime=new_dt)
        assert result is True

        task = mgr._get_task_any_status(tid)
        assert task.status == TaskStatus.PENDING
        assert task.scheduled_time == new_dt

    def test_reactivate_completed_rejected(self):
        db  = make_db()
        pid = make_project(db)
        tid = make_task(db, pid, status=TaskStatus.COMPLETED)
        mgr = TaskManager(db)

        result = mgr.reactivate(tid)
        assert result is False

    def test_reactivate_all_skipped(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        for i in range(4):
            make_task(db, pid, filename=f"s{i}.py", status=TaskStatus.SKIPPED, days_from_now=i+1)

        result = mgr.reactivate_all_skipped(pid)
        assert result.affected == 4

        skipped = mgr.get_tasks_by_status(pid, TaskStatus.SKIPPED)
        assert len(skipped) == 0

    def test_reactivate_all_with_shift(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        original_dt = "2026-04-01 10:00:00"
        t = Task(project_id=pid, file_path="/tmp/f.py",
                 commit_message="feat: old",
                 scheduled_time=original_dt,
                 status=TaskStatus.SKIPPED)
        db.insert_task(t)
        db.update_task_status(t.id if t.id else 1, TaskStatus.SKIPPED)

        # Insère manuellement avec SKIPPED
        with db._connect() as conn:
            conn.execute(
                "UPDATE task_queue SET status='SKIPPED' WHERE project_id=?",
                (pid,)
            )

        result = mgr.reactivate_all_skipped(pid, shift_days=7)
        assert result.affected >= 1

        # Vérifie que la date a bien été décalée
        tasks = mgr.get_tasks_by_status(pid, TaskStatus.PENDING)
        if tasks:
            orig = datetime.strptime(original_dt, "%Y-%m-%d %H:%M:%S")
            new  = datetime.strptime(tasks[0].scheduled_time, "%Y-%m-%d %H:%M:%S")
            diff = (new - orig).days
            assert diff == 7


# ══════════════════════════════════════════════════════════════════════════════
# ── OPÉRATIONS EN MASSE ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestBulkOperations:

    def test_shift_all_positive(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        original_dt = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        t = Task(project_id=pid, file_path="/tmp/f.py",
                 commit_message="feat: test",
                 scheduled_time=original_dt)
        db.insert_task(t)

        result = mgr.shift_all(pid, days_offset=3)
        assert result.affected == 1

        pending = db.get_pending_tasks(pid)
        orig_d = datetime.strptime(original_dt, "%Y-%m-%d %H:%M:%S")
        new_d  = datetime.strptime(pending[0].scheduled_time, "%Y-%m-%d %H:%M:%S")
        assert (new_d - orig_d).days == 3

    def test_shift_all_negative(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        t = Task(project_id=pid, file_path="/tmp/f.py",
                 commit_message="feat: test",
                 scheduled_time=(datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
        db.insert_task(t)

        result = mgr.shift_all(pid, days_offset=-3)
        assert result.affected == 1

    def test_shift_all_from_date_filter(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        # Tâche AVANT la date de filtre → ne doit pas être décalée
        t1 = Task(project_id=pid, file_path="/tmp/f1.py",
                  commit_message="feat: early",
                  scheduled_time="2027-04-01 10:00:00")
        # Tâche APRÈS la date de filtre → doit être décalée
        t2 = Task(project_id=pid, file_path="/tmp/f2.py",
                  commit_message="feat: late",
                  scheduled_time="2027-04-20 10:00:00")
        db.insert_task(t1)
        db.insert_task(t2)

        result = mgr.shift_all(pid, days_offset=5, from_date="2027-04-15")
        assert result.affected == 1
        assert result.skipped  == 1

    def test_set_daily_push_time(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        for i in range(5):
            t = Task(project_id=pid, file_path=f"/tmp/f{i}.py",
                     commit_message=f"feat: {i}",
                     scheduled_time=f"2027-04-{i+1:02d} 09:00:00")
            db.insert_task(t)

        result = mgr.set_daily_push_time(pid, hour=20, minute=30, jitter_min=0)
        assert result.affected == 5

        pending = db.get_pending_tasks(pid)
        for task in pending:
            h = int(task.scheduled_time[11:13])
            m = int(task.scheduled_time[14:16])
            assert h == 20
            assert m == 30

    def test_set_push_days_weekday_constraint(self):
        """Les tâches doivent tomber uniquement sur les jours autorisés."""
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        for i in range(10):
            t = Task(project_id=pid, file_path=f"/tmp/f{i}.py",
                     commit_message=f"feat: {i}",
                     scheduled_time=(datetime.now() + timedelta(days=i+1)).strftime("%Y-%m-%d %H:%M:%S"))
            db.insert_task(t)

        # Seulement lundi (0) et vendredi (4)
        result = mgr.set_push_days(pid, allowed_days=[0, 4], hour=16, minute=0)
        assert result.affected == 10

        pending = db.get_pending_tasks(pid)
        for task in pending:
            dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")
            assert dt.weekday() in [0, 4], \
                f"Jour non autorisé : {dt.strftime('%A')} ({dt.weekday()})"

    def test_swap_schedule(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        t1 = Task(project_id=pid, file_path="/tmp/f1.py",
                  commit_message="feat: first",
                  scheduled_time="2027-04-01 10:00:00")
        t2 = Task(project_id=pid, file_path="/tmp/f2.py",
                  commit_message="feat: second",
                  scheduled_time="2027-04-10 10:00:00")
        id1 = db.insert_task(t1)
        id2 = db.insert_task(t2)

        result = mgr.swap_schedule(id1, id2)
        assert result is True

        task1 = mgr._get_task_any_status(id1)
        task2 = mgr._get_task_any_status(id2)

        assert task1.scheduled_time == "2027-04-10 10:00:00"
        assert task2.scheduled_time == "2027-04-01 10:00:00"

    def test_bulk_edit_messages_changes_prefix(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        for i in range(3):
            t = Task(project_id=pid, file_path=f"/tmp/f{i}.py",
                     commit_message=f"feat: script {i}",
                     scheduled_time=(datetime.now() + timedelta(days=i+1)).strftime("%Y-%m-%d %H:%M:%S"))
            db.insert_task(t)

        result = mgr.bulk_edit_messages(pid, prefix="docs")
        assert result.affected == 3

        pending = db.get_pending_tasks(pid)
        for task in pending:
            assert task.commit_message.startswith("docs:")

    def test_bulk_edit_messages_preserves_suffix(self):
        """Seul le préfixe change, le texte après ':' est conservé."""
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        t = Task(project_id=pid, file_path="/tmp/f.py",
                 commit_message="feat: important feature",
                 scheduled_time=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
        db.insert_task(t)

        mgr.bulk_edit_messages(pid, prefix="fix")

        pending = db.get_pending_tasks(pid)
        assert pending[0].commit_message == "fix: important feature"


# ══════════════════════════════════════════════════════════════════════════════
# ── FILTRES ET RECHERCHE ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestFilters:

    def test_search_by_message(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        make_task(db, pid, filename="auth.py",    message="feat: auth module",   days_from_now=1)
        make_task(db, pid, filename="network.py", message="fix: network error",  days_from_now=2)
        make_task(db, pid, filename="db.py",      message="refactor: database",  days_from_now=3)

        results = mgr.search_tasks(pid, "auth")
        assert len(results) == 1
        assert "auth" in results[0].commit_message

    def test_search_by_filename(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        make_task(db, pid, filename="my_parser.py", days_from_now=1)
        make_task(db, pid, filename="validator.py",  days_from_now=2)

        results = mgr.search_tasks(pid, "parser")
        assert len(results) == 1

    def test_get_tasks_for_day(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        target_day = "2027-06-15"
        t1 = Task(project_id=pid, file_path="/tmp/a.py", commit_message="feat: a",
                  scheduled_time=f"{target_day} 10:00:00")
        t2 = Task(project_id=pid, file_path="/tmp/b.py", commit_message="feat: b",
                  scheduled_time=f"{target_day} 18:00:00")
        t3 = Task(project_id=pid, file_path="/tmp/c.py", commit_message="feat: c",
                  scheduled_time="2027-06-16 10:00:00")

        db.insert_task(t1)
        db.insert_task(t2)
        db.insert_task(t3)

        tasks = mgr.get_tasks_for_day(pid, target_day)
        assert len(tasks) == 2

    def test_get_calendar_summary(self):
        db  = make_db()
        pid = make_project(db)
        mgr = TaskManager(db)

        for day in ["2027-05-01", "2027-05-01", "2027-05-03"]:
            t = Task(project_id=pid, file_path="/tmp/f.py", commit_message="feat: x",
                     scheduled_time=f"{day} 10:00:00")
            db.insert_task(t)

        calendar = mgr.get_calendar_summary(pid)
        assert "2027-05-01" in calendar
        assert len(calendar["2027-05-01"]) == 2
        assert "2027-05-03" in calendar
        assert len(calendar["2027-05-03"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# ── VALIDATION ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestValidation:

    def test_conventional_commit_valid(self):
        assert TaskManager._validate_commit_message("feat: add new feature") is None
        assert TaskManager._validate_commit_message("fix: resolve bug") is None
        assert TaskManager._validate_commit_message("docs: update readme") is None
        assert TaskManager._validate_commit_message("refactor(core): simplify logic") is None

    def test_conventional_commit_missing_prefix(self):
        warning = TaskManager._validate_commit_message("add new feature")
        assert warning is not None
        assert "Conventional Commits" in warning

    def test_conventional_commit_invalid_prefix(self):
        warning = TaskManager._validate_commit_message("wip: work in progress")
        assert warning is not None

    def test_parse_datetime_formats(self):
        parse = TaskManager._parse_datetime

        dt1 = parse("2026-04-15 14:30:00")
        assert dt1.hour == 14 and dt1.minute == 30

        dt2 = parse("2026-04-15 14:30")
        assert dt2.hour == 14 and dt2.minute == 30

        dt3 = parse("2026-04-15")
        assert dt3.hour == 9 and dt3.minute == 0

    def test_parse_datetime_invalid_raises(self):
        with pytest.raises(ValueError):
            TaskManager._parse_datetime("not-a-date")

        with pytest.raises(ValueError):
            TaskManager._parse_datetime("15/04/2026")


# ── Point d'entrée ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])