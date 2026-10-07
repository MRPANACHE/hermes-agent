"""Owning worker timed waits use the native schedule/run/dispatcher path."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
from agent.kanban_stop import build_kanban_stop_nudge, session_called_kanban_terminal
from hermes_cli import goals
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "worker.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    kb.init_db()
    with kb.connect() as conn:
        task = kb.create_task(conn, title="planned cycle verification", assignee="default", goal_mode=True)
        run = kb.claim_task(conn, task)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    return task, run.current_run_id


def args(timestamp=2100):
    return {"wake_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "reason": "Verify the planned cycle after it finishes"}


def receipt(data):
    return [{"role": "tool", "name": "kanban_schedule", "content": json.dumps(data)}]


def test_schedule_native_run_receipt_and_one_time_wake(worker, monkeypatch):
    task, run_id = worker
    result = json.loads(kt._handle_schedule(args()))
    assert result["ok"] is True
    assert result["status"] == "scheduled"
    assert result["run_id"] == run_id
    assert result["task_id"] == task
    assert result["wake_timestamp"] == 2100
    assert build_kanban_stop_nudge(messages=receipt(result)) is None
    with kb.connect() as conn:
        state = kb.get_task(conn, task)
        assert state.status == "scheduled"
        assert state.assignee == "default"
        assert state.current_run_id is None
        assert kb.latest_run(conn, task).outcome == "scheduled"
        assert kb.latest_run(conn, task).summary == args()["reason"]
        assert kb.dispatch_once(conn, max_spawn=0).awakened == []
        monkeypatch.setattr(kb.time, "time", lambda: 2100)
        assert kb.dispatch_once(conn, max_spawn=0).awakened == [task]
        assert kb.dispatch_once(conn, max_spawn=0).awakened == []


def test_schedule_goal_loop_stops_without_judge_completion_or_budget_block(worker, monkeypatch):
    task, _ = worker
    def unexpected(*a, **kw):
        raise AssertionError("scheduled worker must stop without judging or another turn")
    monkeypatch.setattr(goals, "judge_goal", unexpected)
    assert json.loads(kt._handle_schedule(args()))["ok"]
    def status():
        with kb.connect() as conn:
            return kb.get_task(conn, task).status
    result = goals.run_kanban_goal_loop(task_id=task, goal_text="still open",
        first_response="Scheduled exact follow-up", run_turn=unexpected,
        task_status_fn=status, block_fn=unexpected, max_turns=1)
    assert result["outcome"] == "scheduled_by_worker"
    assert result["turns_used"] == 1


@pytest.mark.parametrize("payload", [{}, {"ok": False}, {"error": "timestamp refused"}, {"ok": True, "status": "running"}])
def test_failed_schedule_receipt_keeps_stop_nudge(worker, payload):
    assert not session_called_kanban_terminal(receipt(payload))
    assert build_kanban_stop_nudge(messages=receipt(payload)) is not None


def test_bare_schedule_invocation_keeps_stop_nudge(worker):
    messages = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "kanban_schedule", "arguments": json.dumps(args())}}]}]
    assert not session_called_kanban_terminal(messages)
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize("context", [delegated_child_context, non_dispatcher_owned_context])
def test_child_and_in_process_cron_cannot_schedule_parent(worker, context):
    with context():
        assert not kt._check_kanban_worker_mode()
        assert json.loads(kt._handle_schedule(args())).get("error")
    with kb.connect() as conn:
        assert kb.get_task(conn, worker[0]).status == "running"


def test_orchestrator_cannot_schedule_worker_run(worker, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    assert not kt._check_kanban_worker_mode()
    assert json.loads(kt._handle_schedule(dict(args(), task_id=worker[0]))).get("error")


def test_foreign_task_refused_without_mutation(worker):
    with kb.connect() as conn:
        foreign = kb.create_task(conn, title="foreign", assignee="default")
    result = json.loads(kt._handle_schedule(dict(args(), task_id=foreign)))
    assert "scoped" in result["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, worker[0]).status == "running"
        assert kb.get_task(conn, foreign).status == "ready"


@pytest.mark.parametrize("run_id", [None, "invalid", "0", "-1"])
def test_missing_or_invalid_run_id_refuses_unbound_mutation(worker, monkeypatch, run_id):
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert json.loads(kt._handle_schedule(args())).get("error")
    with kb.connect() as conn:
        assert kb.get_task(conn, worker[0]).status == "running"


def test_old_worker_cannot_schedule_replacement_run(worker):
    task, old_run = worker
    with kb.connect() as conn:
        kb.block_task(conn, task, reason="replace", kind="needs_input", expected_run_id=old_run)
        kb.unblock_task(conn, task)
        new_run = kb.claim_task(conn, task).current_run_id
    assert new_run != old_run
    assert json.loads(kt._handle_schedule(args())).get("error")
    with kb.connect() as conn:
        assert kb.get_task(conn, task).current_run_id == new_run
        assert kb.get_task(conn, task).status == "running"


@pytest.mark.parametrize("bad", [{"wake_at": "tomorrow"}, {"wake_at": "2026-10-07T11:15:00"},
    {"wake_at": ""}, {"wake_at": None}, {"reason": ""}, {"reason": None}, args(2000), args(1999)])
def test_invalid_or_due_wait_refused_without_event_or_run_end(worker, bad):
    data = dict(args(), **bad)
    with kb.connect() as conn:
        prior_events = len(kb.list_events(conn, worker[0]))
    assert json.loads(kt._handle_schedule(data)).get("error")
    with kb.connect() as conn:
        assert kb.get_task(conn, worker[0]).status == "running"
        assert kb.get_task(conn, worker[0]).current_run_id == worker[1]
        assert len(kb.list_events(conn, worker[0])) == prior_events


def test_api_cannot_override_pinned_worker_board(worker):
    assert "board" not in kt.KANBAN_SCHEDULE_SCHEMA["parameters"]["properties"]
    assert json.loads(kt._handle_schedule(dict(args(), board="foreign")))["ok"]
    with kb.connect() as conn:
        assert kb.get_task(conn, worker[0]).status == "scheduled"


def test_registry_exposes_schedule_only_for_owning_worker(worker, monkeypatch):
    from toolsets import resolve_toolset
    from tools.registry import invalidate_check_fn_cache, registry
    def names():
        invalidate_check_fn_cache()
        return {s["function"]["name"] for s in registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)}
    assert "kanban_schedule" in names()
    with delegated_child_context():
        assert "kanban_schedule" not in names()
    with non_dispatcher_owned_context():
        assert "kanban_schedule" not in names()
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    assert "kanban_schedule" not in names()


def test_worker_process_exit_gate_still_applies(worker, monkeypatch):
    task, _ = worker
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), task))
    assert json.loads(kt._handle_schedule(args()))["ok"]
    monkeypatch.setattr(kb.time, "time", lambda: 2100)
    with kb.connect() as conn:
        assert kb.dispatch_once(conn, max_spawn=0).awakened == []
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert kb.dispatch_once(conn, max_spawn=0).awakened == [task]
