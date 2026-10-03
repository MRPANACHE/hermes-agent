"""Canonical reviewer returns permit same-task correction, not duplicate work."""
from pathlib import Path
import time

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as db:
        yield db


def rework(conn):
    tid = kb.create_task(conn, title="same PR correction", assignee="builder")
    impl = kb.claim_task(conn, tid)
    assert impl is not None
    kb.add_comment(conn, tid, author="builder", body="https://github.com/example/repo/pull/1319")
    assert kb.request_review(conn, tid, summary="review", reviewer="reviewer", expected_run_id=impl.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    assert kb.request_changes(conn, tid, reason="fix two findings", expected_run_id=review.current_run_id)[0]
    kb.add_comment(conn, tid, author="owner", body="receipt only; no manual wake")
    return tid


def test_canonical_rework_allows_same_task_claim(conn):
    tid = rework(conn)
    assert kb.check_respawn_guard(conn, tid) is None
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert claimed.id == tid
    assert claimed.assignee == "builder"


@pytest.mark.parametrize("mutation", ["missing_event", "wrong_run", "wrong_owner", "bad_payload", "later_run"])
def test_rework_permission_does_not_outlive_provenance(conn, mutation):
    tid = rework(conn)
    if mutation == "missing_event":
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='changes_requested'", (tid,))
    elif mutation == "wrong_run":
        conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='changes_requested'", (tid,))
    elif mutation == "wrong_owner":
        conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (tid,))
    elif mutation == "bad_payload":
        conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='changes_requested'", (tid,))
    else:
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(conn, tid, reason="transient", kind="transient")
        assert kb.unblock_task(conn, tid)
        kb.add_comment(conn, tid, author="owner", body="newer receipt")
    conn.commit()
    assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_plain_pr_duplicate_remains_guarded(conn):
    tid = kb.create_task(conn, title="ordinary duplicate", assignee="builder")
    kb.add_comment(conn, tid, author="builder", body="https://github.com/example/repo/pull/1319")
    assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_auth_guard_still_precedes_rework(conn):
    tid = rework(conn)
    conn.execute("UPDATE tasks SET last_failure_error='authentication failed' WHERE id=?", (tid,))
    conn.commit()
    assert kb.check_respawn_guard(conn, tid) == "blocker_auth"


@pytest.mark.parametrize("outcome,expected", [("rate_limited", "rate_limit_cooldown"), ("completed", "recent_success")])
def test_terminal_guards_precede_rework(conn, outcome, expected):
    tid = rework(conn)
    conn.execute("UPDATE task_runs SET outcome=?, ended_at=? WHERE id=(SELECT MAX(id) FROM task_runs WHERE task_id=?)", (outcome, int(time.time()) + 1, tid))
    conn.commit()
    assert kb.check_respawn_guard(conn, tid) == expected
