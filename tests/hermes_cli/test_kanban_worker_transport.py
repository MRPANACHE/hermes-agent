"""Mixed-release dispatch selects a transport before any owner admission."""
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.config import atomic_config_write
from tests.hermes_cli.test_kanban_worker_command import _make_task, spawn_env, _write_profile


@pytest.fixture(autouse=True)
def no_install_probe(monkeypatch):
    # This suite exercises transport selection, not PM installation discovery.
    monkeypatch.setattr("tools.environments.local._resolve_hermes_bin_dir", lambda: None)


def test_legacy_profile_uses_its_cli_with_all_task_options(spawn_env, monkeypatch):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", "kanban:\n  worker_transport: cli\n")
    monkeypatch.setattr(Path, "home", lambda: root.parent)
    monkeypatch.setenv("HERMES_BIN", "/trusted/profile-launcher")
    monkeypatch.setattr(dispatch, "_resolve_worker_cli_toolsets", lambda home: ["file", "terminal"])
    task = _make_task()
    task.skills = ["review"]
    task.model_override = "model"
    task.provider_override = "provider"
    task.reasoning_effort = "high"
    dispatch._default_spawn(task, str(workspace), board="mixed")
    assert captured["cmd"] == [
        "/trusted/profile-launcher", "-p", "engine", "--cli", "--accept-hooks",
        "--skills", "review", "-m", "model", "--provider", "provider",
        "--reasoning", "high", "--toolsets", "file,terminal", "chat", "-q",
        "work kanban task t_worker_cmd",
    ]
    env = captured["env"]
    assert env["HERMES_HOME"] == str(root / "profiles" / "engine")
    assert env["HERMES_KANBAN_TASK"] == task.id
    assert env["HERMES_KANBAN_BOARD"] == "mixed"
    assert env["HERMES_KANBAN_RUN_ID"] == "7"
    assert env["HERMES_KANBAN_CLAIM_LOCK"] == "lock"
    assert env["TERMINAL_CWD"] == str(workspace)
    assert str(Path(dispatch.__file__).parents[1]) not in env.get("PYTHONPATH", "").split(":")


def test_dispatcher_cli_setting_does_not_override_assignee_default(spawn_env, monkeypatch):
    root, workspace, captured = spawn_env
    atomic_config_write(root / "config.yaml", {"kanban": {"worker_transport": "cli"}})
    _write_profile(root, "engine", "{}\n")
    monkeypatch.setattr(Path, "home", lambda: root.parent)
    dispatch._default_spawn(_make_task(), str(workspace))
    assert captured["cmd"] == [sys.executable, "-m", "hermes_cli.kanban_worker_client"]


def test_invalid_transport_refuses_before_start(spawn_env, monkeypatch):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", "kanban:\n  worker_transport: clii\n")
    monkeypatch.setattr(Path, "home", lambda: root.parent)
    with pytest.raises(ValueError, match="worker_transport"):
        dispatch._default_spawn(_make_task(), str(workspace))
    assert not captured
