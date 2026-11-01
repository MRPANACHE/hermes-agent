"""An existing owner's PR is execution context, not a second admission policy."""
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "UNKNOWN", "MERGED"])
def test_owner_continues_existing_pr_after_unblock_and_routine_comment(tmp_path, monkeypatch, state):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Finish original delivery", assignee="owner")
        first = kb.claim_task(conn, tid)
        assert first is not None
        kb.add_comment(conn, tid, "owner", f"Continue {state} https://github.com/example/repo/pull/11")
        kb.block_task(conn, tid, kind="needs_input", reason="Missing source", expected_run_id=first.current_run_id)
        kb.unblock_task(conn, tid)
        kb.add_comment(conn, tid, "owner", "Source delivered; scope unchanged")
        with patch("subprocess.run", side_effect=AssertionError("PR lookup is not claim authority")):
            assert kb.check_respawn_guard(conn, tid) is None
            resumed = kb.claim_task(conn, tid)
            assert resumed is not None
            assert resumed.assignee == "owner"
            assert kb.claim_task(conn, tid) is None
        context = kb.build_worker_context(conn, tid)
        assert "https://github.com/example/repo/pull/11" in str(context)
    finally:
        conn.close()


def test_completed_task_is_not_new_work_from_pr_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Completed delivery", assignee="owner")
        run = kb.claim_task(conn, tid)
        assert run is not None
        kb.complete_task(conn, tid, summary="Delivered", expected_run_id=run.current_run_id)
        kb.add_comment(conn, tid, "owner", "https://github.com/example/repo/pull/11")
        assert kb.get_task(conn, tid).status == "done"
        assert kb.claim_task(conn, tid) is None
        assert len(kb.list_runs(conn, tid)) == 1
    finally:
        conn.close()
