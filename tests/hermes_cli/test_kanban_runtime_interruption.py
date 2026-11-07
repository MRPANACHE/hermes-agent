"""Owner transport loss is not a card crash; a lost worker spends no ghost hours."""
import json
import os

import pytest

from tests.hermes_cli.test_kanban_managed_worker_loss import NOW
from hermes_cli import kanban_db_run_limits as limits

pytest_plugins = ["tests.hermes_cli.test_kanban_managed_worker_loss"]


def transport_loss(board, tid, run_id, *, claim=None):
    board.event(tid, run_id, "owner_transport_lost", {
        "reason": "owner_unreachable", "claim_lock": claim or board.state(tid)["claim_lock"],
        "run_id": run_id,
    }, NOW - 97)
    board.conn.commit()


def test_transport_interruption_requeues_without_spending_card_budget(board):
    run_id = board.running("t_transport", worker_lost="EOFError")
    transport_loss(board, "t_transport", run_id)
    board.conn.execute("UPDATE tasks SET max_retries=1 WHERE id='t_transport'")
    board.conn.commit()
    board.tick(failure_limit=1)
    task, run = board.state("t_transport"), board.run(run_id)
    assert task["status"] == "ready"
    assert task["consecutive_failures"] == 0
    assert run["outcome"] == "interrupted"
    assert json.loads(run["metadata"])["infrastructure"] is True
    assert run["ended_at"] == NOW - 96
    assert not board.conn.execute("SELECT 1 FROM task_events WHERE kind='gave_up'").fetchone()


def test_transport_writer_is_bound_to_the_real_submitter(board):
    from hermes_cli.kanban_worker_client import _record_owner_transport_loss
    from hermes_cli import kanban_db as kb
    run_id = board.running("t_writer")
    params = {"task_id": "t_writer", "run_id": run_id,
              "claim_lock": board.state("t_writer")["claim_lock"]}
    _record_owner_transport_loss(kb.kanban_db_path(), params)
    assert not board.conn.execute("SELECT 1 FROM task_events WHERE kind='owner_transport_lost'").fetchone()
    board.conn.execute("UPDATE tasks SET worker_pid=? WHERE id='t_writer'", (os.getpid(),))
    board.conn.commit()
    _record_owner_transport_loss(kb.kanban_db_path(), params)
    stored = board.conn.execute("SELECT payload FROM task_events WHERE kind='owner_transport_lost'").fetchone()
    assert json.loads(stored[0]) == {"reason": "owner_unreachable", "run_id": run_id,
                                     "claim_lock": params["claim_lock"]}


@pytest.mark.asyncio
async def test_silent_owner_records_transport_cause_before_submitter_exit(board, monkeypatch):
    from hermes_cli import gateway_client, kanban_worker_client as client
    from tests.hermes_cli.test_kanban_worker_owner_wait import _owner
    from hermes_cli import kanban_db as kb
    run_id = board.running("t_timeout", worker_pid=os.getpid())
    params = {"task_id": "t_timeout", "run_id": run_id,
              "claim_lock": board.state("t_timeout")["claim_lock"]}
    monkeypatch.setattr(gateway_client, "connect_gateway", _owner(["starting"], []))
    monkeypatch.setattr(client, "OWNER_WAIT_S", 0)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    with pytest.raises(gateway_client.GatewayClientError, match="Kanban owner silent"):
        await client.run(params, kb.kanban_db_path())
    assert board.conn.execute("SELECT 1 FROM task_events WHERE kind='owner_transport_lost'").fetchone()


def test_foreign_claim_transport_note_does_not_exempt_real_crash(board):
    run_id = board.running("t_crash", worker_lost="managed_worker_failed")
    transport_loss(board, "t_crash", run_id, claim="foreign:123")
    board.tick()
    assert board.run(run_id)["outcome"] == "crashed"
    assert board.state("t_crash")["consecutive_failures"] == 1


def test_lost_worker_stops_hour_accounting_before_owner_settlement(board):
    board.running("t_ghost", worker_lost="EOFError", dead_seconds=120)
    assert limits._runtime_seconds(board.conn, NOW, {"start": NOW - 3600, "end": NOW + 3600}) == 24
    assert board.state("t_ghost")["status"] == "running"


def test_unrelated_loss_event_cannot_reduce_runtime(board):
    run_id = board.running("t_live", worker_lost=None)
    board.event("t_live", run_id, "worker_lost", {"claim_lock": "foreign:123"}, NOW - 96)
    board.conn.commit()
    assert limits._runtime_seconds(board.conn, NOW, {"start": NOW - 3600, "end": NOW + 3600}) == 120
