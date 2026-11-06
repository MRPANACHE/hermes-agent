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
