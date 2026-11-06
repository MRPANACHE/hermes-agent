"""Connection setup must share the browse DDL deadline, including macOS PRAGMAs."""
from contextlib import suppress
import sqlite3

import pytest


def test_setup_sql_is_interrupted_before_it_can_escape_the_browse_budget(tmp_path, monkeypatch):
    import hermes_state_reconcile as reconcile
    import hermes_state_repair as repair

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE retained (value TEXT)")
        conn.execute("INSERT INTO retained VALUES ('original')")
    budget = reconcile._Budget(3)
    original_clock = budget._clock
    setup_started = False
    setup_finished = False
    budget._clock = lambda: budget.deadline + 1 if setup_started else original_clock()

    def setup(conn):
        nonlocal setup_started, setup_finished
        setup_started = True
        # Durability setup is best-effort in production and can swallow SQLite's
        # interrupt. The shared budget must still prevent entry into the DDL.
        with suppress(sqlite3.OperationalError):
            conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n").fetchone()
            setup_finished = True

    monkeypatch.setattr(repair, "_reapply_durability_barriers", setup)
    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(path, budget)
    assert setup_started
    assert not setup_finished, "setup SQL ran without the shared progress deadline"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM retained").fetchall() == [("original",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("retained",)]
