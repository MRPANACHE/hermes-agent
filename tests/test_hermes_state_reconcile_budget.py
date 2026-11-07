"""The browse budget also covers opening its additive DDL transaction."""
from contextlib import closing
import sqlite3

import pytest

import hermes_state_reconcile as reconcile


def test_expired_progress_budget_at_begin_is_retryable_and_changes_nothing(tmp_path):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE history (body TEXT)")
        conn.execute("INSERT INTO history VALUES ('keep')")
        conn.commit()

    class ExpiringAtBegin(reconcile._Budget):
        def arm(self, conn):
            def expire():
                self.expired = True
                return 1
            conn.set_progress_handler(expire, 1)

    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(path, ExpiringAtBegin(3))

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT body FROM history").fetchall() == [("keep",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("history",)]


def test_durability_pragmas_are_inside_the_progress_budget(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE history (body TEXT)")
        conn.execute("INSERT INTO history VALUES ('keep')")
        conn.commit()
    budget = reconcile._Budget(3)
    interrupted = []

    def durability(conn):
        # A real SQLite VM is cut off during pre-BEGIN setup, not only later DDL.
        budget.deadline = 0
        try:
            conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n").fetchone()
        except sqlite3.OperationalError as exc:
            interrupted.append(str(exc))
        return False  # The existing durability helper is best-effort and swallows errors.

    monkeypatch.setattr("hermes_state_repair._reapply_durability_barriers", durability)
    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(path, budget)
    assert interrupted == ["interrupted"]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT body FROM history").fetchall() == [("keep",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("history",)]
