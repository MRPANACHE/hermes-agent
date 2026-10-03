"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"



@pytest.mark.asyncio
async def test_kanban_dispatcher_skips_spawns_while_gateway_draining(monkeypatch, tmp_path):
    """A SIGUSR1/restart drain must stop new kanban dispatch before stop()."""
    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": True,
        }
    })
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)

    def should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("dispatcher must not inspect or spawn board work while draining")

    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", should_not_dispatch)

    sleep_calls = 0

    async def fake_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False
        return None

    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)

    class Runner(kw.GatewayKanbanWatchersMixin):
        _running = True
        _draining = True
        _kanban_dispatcher_lock_handle = None

    runner = Runner()
    await runner._kanban_dispatcher_watcher()

    assert runner._kanban_dispatcher_lock_handle is None



@pytest.mark.asyncio
async def test_kanban_dispatcher_rechecks_draining_after_auto_decompose(monkeypatch, tmp_path):
    """If a restart arrives during auto-decompose, the same tick must not spawn."""
    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": True,
        }
    })
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda _load_config: (True, 1))

    sleep_calls = 0

    async def fake_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False
        return None

    async def fake_to_thread(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_auto_decompose_tick":
            runner._draining = True
            return 0
        if getattr(fn, "__name__", "") == "_tick_once":
            raise AssertionError("dispatch tick must be skipped after drain begins")
        return fn(*args, **kwargs)

    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)

    class Runner(kw.GatewayKanbanWatchersMixin):
        _running = True
        _draining = False
        _kanban_dispatcher_lock_handle = None
        _kanban_dispatch_active_count = 0

    runner = Runner()
    await runner._kanban_dispatcher_watcher()

    assert runner._kanban_dispatch_active_count == 0
