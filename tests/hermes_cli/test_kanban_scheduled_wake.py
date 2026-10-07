"""Durable timed resumption through the native Kanban event/dispatcher path."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def at(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def tick(conn, **kwargs):
    return kb.dispatch_once(conn, max_spawn=0, **kwargs)


def test_due_schedule_survives_connection_and_dispatches_once(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="resume original", assignee="otto")
        kb.claim_task(conn, task)
        original_run = kb.get_task(conn, task).current_run_id
        assert kb.schedule_task(conn, task, wake_at=at(2001), reason="after cycle")
        assert tick(conn).awakened == []
    monkeypatch.setattr(kb.time, "time", lambda: 2001)
    with kb.connect() as conn:
        assert tick(conn).awakened == [task]
        assert kb.get_task(conn, task).status == "ready"
        assert kb.get_task(conn, task).assignee == "otto"
        assert tick(conn).awakened == []
        wakes = [e for e in kb.list_events(conn, task) if e.kind == "schedule_woke"]
        assert len(wakes) == 1
        assert wakes[0].run_id == original_run


def test_parent_gate_survives_timer_and_later_promotes(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent], assignee="otto")
        kb.schedule_task(conn, child, wake_at=at(1999))
        assert tick(conn).awakened == [child]
        assert kb.get_task(conn, child).status == "todo"
        kb.complete_task(conn, parent)
        tick(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert tick(conn).awakened == []


def test_legacy_manual_and_input_blocks_never_get_timer(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        manual = kb.create_task(conn, title="manual")
        blocked = kb.create_task(conn, title="operator")
        kb.schedule_task(conn, manual, reason="tomorrow in prose")
        kb.block_task(conn, blocked, reason="needs operator", kind="needs_input")
        assert tick(conn).awakened == []
        assert kb.get_task(conn, manual).status == "scheduled"
        assert kb.get_task(conn, blocked).status == "blocked"


def test_latest_reschedule_replaces_prior_due_time(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="reschedule")
        kb.schedule_task(conn, task, wake_at=at(1999), reason="original")
        original = [e for e in kb.list_events(conn, task) if e.kind == "scheduled"][-1]
        assert kb.schedule_task(conn, task, wake_at=at(2001), reason="later")
        latest = [e for e in kb.list_events(conn, task) if e.kind == "scheduled"][-1]
        assert latest.run_id == original.run_id
        assert tick(conn).awakened == []
        monkeypatch.setattr(kb.time, "time", lambda: 2001)
        assert tick(conn).awakened == [task]


def test_due_worker_schedule_waits_until_process_exits_even_after_reschedule(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="worker still exiting")
        kb.claim_task(conn, task)
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), task))
        kb.schedule_task(conn, task, wake_at=at(1999))
        assert kb.schedule_task(conn, task, wake_at=at(1998))
        assert tick(conn).awakened == []
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert tick(conn).awakened == [task]


def test_review_timer_retains_phase_through_open_parent(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        task = kb.create_task(conn, title="review", parents=[parent])
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task,))
        assert kb.schedule_task(conn, task, wake_at=at(1999))
        assert tick(conn).awakened == [task]
        assert kb.get_task(conn, task).status == "todo"
        kb.complete_task(conn, parent)
        tick(conn)
        assert kb.get_task(conn, task).status == "review"


def test_run_cas_refuses_stale_worker_without_scheduling(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="run owned")
        kb.claim_task(conn, task)
        run_id = kb.get_task(conn, task).current_run_id
        assert not kb.schedule_task(conn, task, wake_at=at(1999), expected_run_id=run_id + 1)
        assert kb.get_task(conn, task).current_run_id == run_id
        assert kb.get_task(conn, task).status == "running"


def test_timer_waits_for_no_claim_and_no_active_run(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="active claim")
        kb.schedule_task(conn, task, wake_at=at(1999))
        conn.execute("UPDATE tasks SET claim_lock='active' WHERE id=?", (task,))
        assert tick(conn).awakened == []
        conn.execute("UPDATE tasks SET claim_lock=NULL, current_run_id=12345 WHERE id=?", (task,))
        assert tick(conn).awakened == []


def test_dry_dispatch_does_not_consume_timer(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="dry")
        kb.schedule_task(conn, task, wake_at=at(1999))
        assert tick(conn, dry_run=True).awakened == []
        assert kb.get_task(conn, task).status == "scheduled"
        assert tick(conn).awakened == [task]


def test_due_dispatch_spawns_original_once_and_does_not_retry_new_input_block(home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    calls = []
    def spawn(task, workspace, board):
        calls.append((task.id, task.assignee))
    with kb.connect() as conn:
        task = kb.create_task(conn, title="original scheduled card", assignee="default")
        kb.schedule_task(conn, task, wake_at=at(1999))
        first = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert first.awakened == [task]
        assert calls == [(task, "default")]
        assert kb.get_task(conn, task).status == "running"
        kb.block_task(conn, task, reason="human input still needed", kind="needs_input")
        assert kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1).awakened == []
        assert calls == [(task, "default")]
        assert kb.get_task(conn, task).status == "blocked"


@pytest.mark.parametrize("timestamp", ["tomorrow", "2026-10-07T11:15:00", "2026-10-07"])
def test_invalid_time_refused_before_cli_comments_or_mutation(home, capsys, timestamp):
    with kb.connect() as conn:
        task = kb.create_task(conn, title="invalid")
    assert cli._cmd_schedule(argparse.Namespace(task_id=task, reason=["test"], at=timestamp)) == 1
    assert "timezone" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_task(conn, task).status == "ready"
        assert kb.list_comments(conn, task) == []


def test_offsets_represent_same_instant():
    assert kb.parse_schedule_time("2026-10-07T11:15:00+02:00") == kb.parse_schedule_time("2026-10-07T09:15:00Z")


@pytest.mark.parametrize("payload", ["invalid json", "[]", '{"wake_at": "2000"}', '{"wake_at": true}', '{"wake_at":1999,"worker_pid":"bad"}'])
def test_malformed_schedule_cannot_break_tick_or_unblock(home, monkeypatch, payload):
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="bad receipt")
        kb.schedule_task(conn, task, wake_at=at(1999))
        conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='scheduled'", (payload, task))
        assert tick(conn).awakened == []
        assert kb.get_task(conn, task).status == "scheduled"


def test_real_cli_schedule_dispatch_receipt(home):
    root = Path(__file__).parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    env["PYTHONPATH"] = str(root)
    def run(*args):
        result = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout
    task = json.loads(run("create", "real CLI timer", "--assignee", "otto", "--json"))["id"]
    run("schedule", task, "after cycle", "--at", "2000-01-01T12:00:00+01:00")
    first = json.loads(run("dispatch", "--max", "0", "--json"))
    second = json.loads(run("dispatch", "--max", "0", "--json"))
    assert first["awakened"] == [task]
    assert second["awakened"] == []
    with kb.connect() as conn:
        assert kb.get_task(conn, task).status == "ready"
        assert kb.get_task(conn, task).assignee == "otto"
        assert len([e for e in kb.list_events(conn, task) if e.kind == "schedule_woke"]) == 1
