"""Native board workers and dispatch activity must drain before a gateway reload."""

import asyncio
import sqlite3

import pytest

from gateway.run_shutdown import GatewayShutdownMixin


def test_running_cards_are_counted_once_per_database(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (status TEXT, assignee TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, ?)", [
            ("running", "mara"), ("running", "otto"), ("ready", "mara"),
        ])
    monkeypatch.setattr(kb, "list_boards", lambda **kwargs: [{"slug": "a"}, {"slug": "b"}])
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kwargs: path)
    runner = GatewayShutdownMixin()
    runner._primary_profile_name = "mara"
    assert runner._active_kanban_worker_count() == 1
    runner._served_profile_homes = {"otto": tmp_path / "otto"}
    assert runner._active_kanban_worker_count() == 2
    path.write_bytes(b"unreadable sqlite")
    assert runner._active_kanban_worker_count() == 1


def test_foreign_running_card_does_not_block_but_unknown_owner_does(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (status TEXT, assignee TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('running', 'otto')")
    monkeypatch.setattr(kb, "list_boards", lambda **kwargs: [{"slug": "default"}])
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kwargs: path)
    runner = GatewayShutdownMixin()
    runner._primary_profile_name = "mara"
    assert runner._active_kanban_worker_count() == 0
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO tasks VALUES ('running', ?)", [(None,), ("",)])
    assert runner._active_kanban_worker_count() == 2


@pytest.mark.asyncio
async def test_drain_waits_for_dispatch_and_worker_completion():
    class Runner(GatewayShutdownMixin):
        _running_agents = {}
        _kanban_dispatch_active_count = 1
        workers = 1

        def _running_agent_count(self):
            return 0

        def _active_cron_job_count(self):
            return 0

        def _active_api_run_count(self):
            return 0

        def _active_deferred_agent_worker_count(self):
            return 0

        def _active_kanban_worker_count(self):
            return self.workers

        def _snapshot_running_agents(self):
            return {}

        def _update_runtime_status(self, status):
            pass

    runner = Runner()
    assert runner._active_work_count() == 2

    async def complete_work():
        await asyncio.sleep(0)
        runner._kanban_dispatch_active_count = 0
        runner.workers = 0

    completion = asyncio.create_task(complete_work())
    _, timed_out = await runner._drain_active_agents(2.0)
    await completion
    assert timed_out is False
    assert runner._active_work_count() == 0
