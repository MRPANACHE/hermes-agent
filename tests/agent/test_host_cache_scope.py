"""Host-declared process cache scope (``HERMES_PROMPT_CACHE_SCOPE``).

A batch runner starts one CLI process, and so one session, per batch. Without a shared scope every
batch hashes its own session id into ``prompt_cache_key`` and lands on a different bucket, although
the instructions are identical across the run.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.prompt_cache_scope import HOST_CACHE_SCOPE_ENV, resolve_prompt_cache_scope
from agent.transports.codex import _content_cache_key


def _agent(session_id, **extra):
    return SimpleNamespace(session_id=session_id, _session_db=None, provider="openai-codex",
                           model="gpt-6.1-terra", base_url="https://chatgpt.com/backend-api/codex", **extra)


def test_parallel_sessions_share_the_host_scope_and_key(monkeypatch):
    monkeypatch.setenv(HOST_CACHE_SCOPE_ENV, "bbs:run-1")
    first, second = _agent("batch-a"), _agent("batch-b")

    assert resolve_prompt_cache_scope(first) == resolve_prompt_cache_scope(second) == "bbs:run-1"
    assert _content_cache_key("brief", None, resolve_prompt_cache_scope(first)) == _content_cache_key(
        "brief", None, resolve_prompt_cache_scope(second))


def test_different_instructions_keep_different_keys_under_one_scope(monkeypatch):
    monkeypatch.setenv(HOST_CACHE_SCOPE_ENV, "bbs:run-1")
    scope = resolve_prompt_cache_scope(_agent("batch-a"))

    assert _content_cache_key("brief", None, scope) != _content_cache_key("other", None, scope)


def test_unset_or_blank_keeps_the_session_scope(monkeypatch):
    monkeypatch.delenv(HOST_CACHE_SCOPE_ENV, raising=False)
    assert resolve_prompt_cache_scope(_agent("batch-a")) == "batch-a"
    monkeypatch.setenv(HOST_CACHE_SCOPE_ENV, "  ")
    assert resolve_prompt_cache_scope(_agent("batch-b")) == "batch-b"


def test_slot_keyed_route_ignores_the_host_scope(monkeypatch):
    monkeypatch.setenv(HOST_CACHE_SCOPE_ENV, "bbs:run-1")
    agent = SimpleNamespace(session_id="batch-a", _session_db=None, provider="xai", model="grok-5", base_url="")

    assert resolve_prompt_cache_scope(agent) == "batch-a"


def test_inherited_fork_scope_still_wins(monkeypatch):
    monkeypatch.setenv(HOST_CACHE_SCOPE_ENV, "bbs:run-1")

    assert resolve_prompt_cache_scope(_agent("fork", _inherited_cache_scope="gwk_parent")) == "gwk_parent"
