"""Persisted plugin sections must survive the actual profile-aware renderer."""

from pathlib import Path

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.system_prompt import (
    _active_profile_line,
    build_system_prompt,
    invalidate_system_prompt,
    restore_plugin_prompt_sections,
)
from hermes_cli import plugins
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_state import SessionDB
from run_agent import AIAgent


def _agent(session_id, db=None):
    return AIAgent(
        api_key="test-key", base_url="https://openrouter.ai/api/v1",
        model="test/model", provider="openrouter", platform="cli",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        session_id=session_id, session_db=db,
    )


@pytest.fixture(params=["default", "orchid"])
def profile_home(request, tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    home = root if request.param == "default" else root / "profiles" / request.param
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("agent.coding_context._coding_mode", lambda: "off")
    return home


def _section(monkeypatch, callback):
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="example", key="example", source="user"), manager)
    context.register_system_prompt_section("example.original", callback, position="after_memory")
    monkeypatch.setattr(plugins, "_plugin_manager", manager)


def test_real_renderer_resume_restores_original_sections_before_rebuild(profile_home, monkeypatch):
    state = {"generation": "original A 🌱", "calls": 0}

    def render(info):
        state["calls"] += 1
        return state["generation"]

    _section(monkeypatch, render)
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        db.ensure_session("native-section-resume", source="cli", model="test/model")
        first = _agent("native-section-resume", db)
        _restore_or_build_system_prompt(first, None, [])
        original = first._cached_system_prompt
        assert _active_profile_line(first) in original
        state["generation"] = "current B"

        resumed = _agent("native-section-resume", db)
        _restore_or_build_system_prompt(resumed, None, [{"role": "user", "content": "continue"}])
        assert resumed._cached_system_prompt == original
        assert [section.content for section in resumed._plugin_system_prompt_sections_snapshot] == ["original A 🌱"]
        assert state["calls"] == 1

        # Compression keeps the recovered snapshot as previous input, then rerenders normally.
        invalidate_system_prompt(resumed)
        assert [section.content for section in resumed._plugin_system_prompt_sections_previous] == ["original A 🌱"]
        rebuilt = build_system_prompt(resumed)
        assert "current B" in rebuilt
        assert state["calls"] == 2
    finally:
        db.close()


def test_legacy_tail_and_rejected_profile_frame_and_length(profile_home, monkeypatch):
    _section(monkeypatch, lambda info: "original A 🌱")
    agent = _agent("native-section-validation")
    prompt = build_system_prompt(agent)
    line = _active_profile_line(agent)
    legacy = prompt.replace(line + "\n\n", "", 1)
    restore_plugin_prompt_sections(agent, legacy)
    assert [section.content for section in agent._plugin_system_prompt_sections_snapshot] == ["original A 🌱"]

    malformed = [
        prompt.replace(line, "Active Hermes profile: other. Different profile.", 1),
        prompt.replace("\n\nConversation started:", "\n\nUser text:", 1),
        prompt.replace("hermes-plugin-sections:end", "wrong-frame:end", 1),
        prompt.replace("section-chars:12", "section-chars:4001", 1),
    ]
    for rejected in malformed:
        assert rejected != prompt
        restore_plugin_prompt_sections(agent, rejected)
        assert agent._plugin_system_prompt_sections_snapshot == ()
