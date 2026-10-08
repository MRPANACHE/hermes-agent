"""Drain/ESTOP keep running lifecycle maintenance, never admit new work."""

from datetime import datetime, timezone
from pathlib import Path
import signal

import pytest

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["drain", "estop"])
async def test_paused_gateway_maintains_workers_without_admission(tmp_path, monkeypatch, gate):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = 100000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    kb.init_db()
    host = kb._claimer_id().split(":", 1)[0]
    signals = []
    live_pid, deadline_pid, crash_pid = 910001, 910002, 910003
    alive = {live_pid, deadline_pid}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in alive)

    def record_signal(pid, sig):
        signals.append((pid, sig))
        alive.discard(pid)

    # Only simulated fixture PIDs can be signalled. Production signal rules
    # themselves are exercised unchanged, including the normal TERM grace.
    monkeypatch.setattr(kb.os, "kill", record_signal)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "dispatch_stale_timeout_seconds": 14400,
            "auto_decompose": True,
        }
    })
    if gate == "estop":
        from agent.estop import engage
        engage(reason="test housekeeping")

    def forbidden(*args, **kwargs):
        raise AssertionError("paused dispatcher must not admit new execution")

    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", forbidden)
    monkeypatch.setattr(kb, "_default_spawn", forbidden)
    monkeypatch.setattr(kb, "wake_scheduled_tasks", forbidden)
    monkeypatch.setattr(kb, "recompute_ready", forbidden)

    with kb.connect() as conn:
        def running(title, pid, age, budget=None, remote=False):
            tid = kb.create_task(conn, title=title, assignee="otto", max_runtime_seconds=budget)
            claimer = "other-host:worker" if remote else f"{host}:worker"
            kb.claim_task(conn, tid, claimer=claimer, ttl_seconds=100000)
            kb._set_worker_pid(conn, tid, pid)
            conn.execute("UPDATE tasks SET started_at=?, last_heartbeat_at=? WHERE id=?", (now-age, now, tid))
            conn.execute("UPDATE task_runs SET started_at=?, last_heartbeat_at=? WHERE task_id=?", (now-age, now, tid))
            conn.commit()
            return tid

        live = running("old worker, fresh heartbeat", live_pid, 20000, 30000)
        deadline = running("deadline elapsed", deadline_pid, 120, 60)
        crash = running("old crashed worker", crash_pid, 120)
        remote = running("remote owner not ours", 910004, 120, 60, remote=True)
        kb._record_worker_exit(crash_pid, 1 << 8)
        ready = kb.create_task(conn, title="ready", assignee="otto")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (ready,))
        conn.commit()
        review = kb.create_task(conn, title="review", assignee="otto")
        kb.request_review(conn, review, summary="fixture")
        scheduled = kb.create_task(conn, title="due timer", assignee="otto")
        kb.schedule_task(conn, scheduled, wake_at=datetime.fromtimestamp(now-1, timezone.utc).isoformat())
        before = {tid: kb.get_task(conn, tid) for tid in [live, remote, ready, review, scheduled]}
        spawn_events_before = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='spawned'").fetchone()[0]

    class Runner(kw.GatewayKanbanWatchersMixin):
        _running = True
        _draining = gate == "drain"
        _kanban_dispatcher_lock_handle = None

    runner = Runner()
    dispatch = kb.dispatch_once
    active_counts = []

    def observed_dispatch(conn, **kwargs):
        active_counts.append(runner._kanban_dispatch_active_count)
        assert runner._kanban_dispatch_active_count == 1
        return dispatch(conn, **kwargs)

    monkeypatch.setattr(kb, "dispatch_once", observed_dispatch)

    async def stop_after_tick(delay):
        if delay != 5:  # Leave the watcher's initial startup delay as a no-op.
            runner._running = False

    monkeypatch.setattr(kw.asyncio, "sleep", stop_after_tick)
    try:
        await runner._kanban_dispatcher_watcher()
    finally:
        runner._release_kanban_dispatcher_lock()
    assert active_counts == [1]
    assert runner._kanban_dispatch_active_count == 0

    with kb.connect() as conn:
        for tid, previous in before.items():
            current = kb.get_task(conn, tid)
            assert current.status == previous.status
            assert current.claim_lock == previous.claim_lock
            assert current.current_run_id == previous.current_run_id
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='spawned'").fetchone()[0] == spawn_events_before
        assert kb.get_task(conn, crash).status != "running"
        assert kb.get_task(conn, deadline).status != "running"
        assert any(e.kind == "crashed" for e in kb.list_events(conn, crash))
        assert any(e.kind == "timed_out" for e in kb.list_events(conn, deadline))
        assert kb.get_task(conn, live).worker_pid == live_pid
        assert signals == [(deadline_pid, signal.SIGTERM)]
