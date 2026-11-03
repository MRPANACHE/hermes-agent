"""Parked owners stay ready without a worker or a native run."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli import profiles


@pytest.mark.parametrize("allowlist", [False, True])
def test_parked_owner_is_not_claimed_and_unpark_is_immediate(tmp_path, monkeypatch, allowlist):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    owner = home / "profiles" / "kestrel"
    owner.mkdir(parents=True)
    (owner / "config.yaml").write_text("model: test\n")
    if allowlist:
        (home / "config.yaml").write_text("kanban:\n  dispatch_profiles: [kestrel]\n")
    marker = profiles.parked_marker_path(owner)
    marker.touch()
    kb.init_db()
    called = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ordinary owner work", assignee="kestrel")
        gate = kbd._profile_exists_fn()
        assert gate("kestrel") is False
        assert kbd.has_spawnable_ready(conn) is False
        for _ in range(2):
            result = kbd.dispatch_once(conn, spawn_fn=lambda *args: called.append(args))
            assert result.spawned == []
            assert result.skipped_nonspawnable == [tid]
        assert called == []
        assert kb.get_task(conn, tid).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        marker.unlink()
        assert gate("kestrel") is True
        assert kbd.has_spawnable_ready(conn) is True
        result = kbd.dispatch_once(conn, dry_run=True)
        assert [task for task, _, _ in result.spawned] == [tid]


def test_default_marker_does_not_park_host(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles.parked_marker_path(home).touch()
    assert kbd._profile_exists_fn()("default") is True
