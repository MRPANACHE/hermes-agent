"""Native measurement waits must park without inventing a timer or bypassing ownership."""
import json

import pytest

from tests.tools import test_kanban_tools as fixtures

worker_env = fixtures.worker_env


def test_measurement_wait_stays_parked_without_timed_dispatch(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools
    from tools.registry import registry

    entry = registry.get_entry("kanban_schedule")
    assert entry is not None
    assert "wake_at" not in entry.schema["parameters"]["required"]
    result = json.loads(entry.handler({
        "reason": "OOS measures the original source independently",
        "await_measurement": True,
        "wait_only": False,
    }))
    assert result["ok"] and result["status"] == "scheduled"
    assert result["wake_at"] is None and result["wake_timestamp"] is None
    with kbc.connect() as conn:
        event = next(e for e in kb.list_events(conn, worker_env) if e.kind == "scheduled")
        assert event.payload is not None
        assert "wake_at" not in event.payload
        assert event.payload["wait_only"] is False
        run = kb.latest_run(conn, worker_env)
        assert run is not None and run.outcome == "scheduled"
        monkeypatch.setattr(kb.time, "time", lambda: 4_000_000_000)
        assert kb.dispatch_once(conn, max_spawn=0).awakened == []
        task = kb.get_task(conn, worker_env)
        assert task is not None and task.status == "scheduled"


@pytest.mark.parametrize("options", [
    {},
    {"await_measurement": "true"},
    {"await_measurement": True, "wait_only": "false"},
    {"await_measurement": True, "wake_at": "2099-01-01T00:00:00+00:00"},
])
def test_invalid_wait_does_not_mutate_native_task(worker_env, options):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools
    from tools.registry import registry

    entry = registry.get_entry("kanban_schedule")
    assert entry is not None
    result = json.loads(entry.handler({"reason": "Independent measurement", **options}))
    assert "error" in result
    with kbc.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None and task.status == "running"
        assert not any(e.kind == "scheduled" for e in kb.list_events(conn, worker_env))


def test_measurement_wait_preserves_owner_and_run_fences(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    from tools.registry import registry

    entry = registry.get_entry("kanban_schedule")
    assert entry is not None
    options = {"reason": "OOS measures independently", "await_measurement": True}
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="unrelated", assignee="peer")
    foreign = json.loads(entry.handler({**options, "task_id": sibling}))
    assert "refusing to mutate" in foreign["error"]
    with monkeypatch.context() as context:
        context.setenv("HERMES_KANBAN_RUN_ID", "999999999")
        stale = json.loads(entry.handler(options))
        assert "error" in stale
    monkeypatch.setattr(kt, "_delegation_ctx", lambda predicate, default: predicate == "is_delegated_child_process_context")
    delegated = json.loads(entry.handler(options))
    assert "delegate_task child agents are not Kanban run owners" in delegated["error"]
    with kbc.connect() as conn:
        task = kb.get_task(conn, worker_env)
        other = kb.get_task(conn, sibling)
        assert task is not None and task.status == "running"
        assert other is not None and other.status == "ready"
