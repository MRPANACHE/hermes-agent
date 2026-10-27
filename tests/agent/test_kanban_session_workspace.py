"""Local native workers must retain their actual workspace, not a claimed one."""

import os
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent, _launch_cwd_for_session


@pytest.mark.parametrize("source", ["cli", "oneshot", "kanban"])
def test_local_execution_records_actual_cwd(tmp_path, monkeypatch, source):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.chdir(tmp_path)
    assert _launch_cwd_for_session(source) == os.getcwd()


@pytest.mark.parametrize("source", ["cron", "telegram", "api_server", "subagent"])
def test_other_sources_do_not_acquire_a_host_workspace(monkeypatch, source):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert _launch_cwd_for_session(source) is None


@pytest.mark.parametrize("backend", ["docker", "ssh", "modal"])
def test_remote_kanban_does_not_record_host_cwd(monkeypatch, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    assert _launch_cwd_for_session("kanban") is None


def test_unlinked_kanban_cwd_remains_unknown(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    with patch("run_agent.os.getcwd", side_effect=FileNotFoundError):
        assert _launch_cwd_for_session("kanban") is None


def test_native_kanban_row_retains_launch_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "worker-worktree"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    # A caller-provided terminal path must not substitute for the actual launch cwd.
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "unrelated"))
    monkeypatch.chdir(workspace)
    db = SessionDB(db_path=home / "state.db")
    try:
        # Exercise the real lazy row writer without a model/provider/network call.
        agent = AIAgent.__new__(AIAgent)
        agent._session_db = db
        agent._session_db_created = False
        agent.session_id = "local-native-worker"
        agent.platform = "cli"
        agent.model = "no-provider-used"
        agent._session_init_model_config = {}
        agent._cached_system_prompt = "unchanged stored input"
        agent._parent_session_id = None
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="test-worker"):
            agent._ensure_db_session()
        assert agent._session_db_created
        row = db.get_session(agent.session_id)
        assert row["source"] == "kanban"
        assert row["profile_name"] == "test-worker"
        assert row["cwd"] == os.getcwd()
        # An idempotent retry must not rewrite original workspace/history.
        monkeypatch.chdir(tmp_path)
        agent._ensure_db_session()
        assert db.get_session(agent.session_id)["cwd"] == str(workspace)
    finally:
        db.close()
