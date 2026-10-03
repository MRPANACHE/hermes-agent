"""Input/access blocks cannot authorize automatic triage work or new children."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as specify


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_limited_resume_preserves_block_result_and_identity_after_reopen(kanban_home, kind):
    reason = "Dolly to Brain access is still missing"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="Enable existing Brain read route", assignee="pax",
            idempotency_key="dolly-pax-enable-brain-read-route-test",
        )
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, tid, claimer="pax") is not None
        # Preserve the successful CI repair independently of the open blocker.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("CI repaired", tid))
        assert kb.block_task(conn, tid, reason=reason, kind=kind)
        kb.add_comment(conn, tid, author="dolly", body="Resume only to repair CI")
        assert kb.unblock_task(conn, tid)
        claimed = kb.claim_task(conn, tid, claimer="pax")
        assert claimed is not None
        assert kb.block_task(
            conn, tid, reason=reason, kind=kind, expected_run_id=claimed.current_run_id,
        )
        before = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        events = kb.list_events(conn, tid)
        assert before.status == "blocked"
        assert before.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert before.current_run_id is None
        assert runs[-1].summary == reason
        assert runs[-1].outcome == "blocked"
        assert events[-2].kind == "block_loop_detected"
        assert events[-2].payload["target_status"] == "blocked"
        assert events[-1].kind == "blocked"
        assert events[-1].payload["loop_detected"] is True
        assert events[-1].payload["reason"] == reason
        assert events[-1].run_id == runs[-1].id

    # A fresh connection/init models process restart without touching live data.
    kb.init_db()
    with patch("agent.auxiliary_client.call_llm") as auxiliary:
        assert tid not in decomp.list_triage_ids()
        assert not decomp.decompose_task(tid).ok
        auxiliary.assert_not_called()
    with kb.connect_closing() as conn:
        assert kb.create_task(
            conn, title="Repeated original request", assignee="pax",
            idempotency_key="dolly-pax-enable-brain-read-route-test",
        ) == tid
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid) == before
        assert kb.list_runs(conn, tid) == runs
        assert kb.list_events(conn, tid) == events
        assert kb.claim_task(conn, tid, claimer="pax") is None
        assert len(kb.list_tasks(conn)) == 1


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_historical_access_triage_rejected_without_model_or_mutations(kanban_home, kind):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Historical blocked card", assignee="pax", triage=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 2, result = ? WHERE id = ?",
                (kind, "CI repaired", tid),
            )
        before = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    with patch("agent.auxiliary_client.call_llm") as auxiliary:
        assert tid not in decomp.list_triage_ids()
        assert tid not in specify.list_triage_ids()
        assert not decomp.decompose_task(tid).ok
        assert not specify.specify_task(tid).ok
        auxiliary.assert_not_called()
    with kb.connect_closing() as conn:
        # Direct callers and races after preflight cannot bypass the DB boundary.
        assert not kb.specify_triage_task(conn, tid, title="Replacement", assignee="emma")
        assert kb.decompose_triage_task(
            conn, tid, root_assignee="emma", children=[{"title": "New work"}],
        ) is None
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid) == before
        assert kb.list_events(conn, tid) == events
        assert kb.list_runs(conn, tid) == []
        assert len(kb.list_tasks(conn)) == 1


@pytest.mark.parametrize("kind", [None, "transient"])
def test_generic_loop_still_routes_to_triage(kanban_home, kind):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Generic failure", assignee="worker")
        kb.recompute_ready(conn)
        for attempt in range(2):
            if attempt:
                assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(conn, tid, reason="Temporary failure", kind=kind)
        assert kb.get_task(conn, tid).status == "triage"
    assert tid in decomp.list_triage_ids()


def test_fresh_triage_still_decomposes(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Fresh authorized work", triage=True)
        ids = kb.decompose_triage_task(
            conn, tid, root_assignee="emma", children=[{"title": "Implement", "assignee": "pax"}],
        )
        assert ids is not None
        assert kb.get_task(conn, ids[0]).status == "ready"
        assert kb.get_task(conn, tid).status == "todo"
