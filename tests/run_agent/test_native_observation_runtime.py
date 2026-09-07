"""Actual constructor, conversation wrapper and authorized dispatch; synthetic engine."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest


@pytest.fixture
def make_agent(monkeypatch):
    from run_agent import AIAgent

    def initialize(agent, **kwargs):
        agent.session_id = kwargs.get("session_id") or "session-1"
        agent.platform = "test"
        agent.model = "synthetic"
        agent._session_db = None
        agent._parent_session_id = None
        agent._conversation_root_id = lambda: agent.session_id
        agent._reset_activity_labels_after_turn = lambda: None
        agent._touch_activity = lambda *_args: None
        agent._tool_guardrails = SimpleNamespace(before_call=lambda *_args: SimpleNamespace(allows_execution=True))
        agent.quiet_mode = True
        agent.tool_progress_callback = None
        agent.tool_start_callback = None
        agent._current_turn_id = "turn-1"
        agent._current_api_request_id = "api-1"
        agent.log_prefix = ""
        agent.status_callback = None

    monkeypatch.setattr("agent.agent_init.init_agent", initialize)
    return lambda factory=None, session="session-1": AIAgent(session_id=session, native_observation_scope_factory=factory)


def factory_for(calls, *, stale_session=None):
    from tools.mcp_observation import mcp_observation_scope

    async def sink(_value):
        pass

    def factory(metadata):
        calls.append(dict(metadata))
        with pytest.raises(TypeError):
            metadata["execution_id"] = "caller-mutation"
        return mcp_observation_scope(agent_run_id="approved-run", runtime_owner="hermes:magnus",
                                     native_session_id=stale_session or metadata["native_session_id"],
                                     execution_id=metadata["execution_id"], intent_id="approved-intent",
                                     server_name="magnus", sink=sink)

    return factory


def dispatch(agent, execute=None, *, blocked=None):
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from tools.mcp_observation import snapshot_mcp_observation

    def default_execute(_args):
        capture = snapshot_mcp_observation("magnus", "procurement_mail_read", {})
        return json.loads(capture.input_json) if capture else {"unscoped": True}

    return _run_agent_tool_execution_middleware(
        agent, function_name="mcp_magnus_procurement_mail_read", function_args={"agent_run_id": "model-forgery"},
        effective_task_id="task-1", tool_call_id="tool-call-1", execute=execute or default_execute,
        scope_block=blocked,
    ).result


def engine(monkeypatch, callback):
    def run(agent, *_args, **_kwargs):
        return {"final_response": callback(agent), "messages": [], "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run)


def test_constructor_rejects_invalid_factory_before_initialization(monkeypatch):
    from run_agent import AIAgent
    initialize = Mock()
    monkeypatch.setattr("agent.agent_init.init_agent", initialize)
    with pytest.raises(TypeError, match="native_observation_scope_factory_invalid"):
        AIAgent(native_observation_scope_factory="from-config-or-model")
    initialize.assert_not_called()


def test_actual_conversation_and_dispatch_use_current_immutable_metadata(make_agent, monkeypatch):
    calls = []
    agent = make_agent(factory_for(calls))
    engine(monkeypatch, dispatch)
    first = agent.run_conversation("synthetic turn")["final_response"]
    second = agent.run_conversation("later turn")["final_response"]
    assert first["agent_run_id"] == "approved-run"
    assert first["intent_id"] == "approved-intent"
    assert first["execution_id"] != second["execution_id"]
    assert str(UUID(first["execution_id"])) == first["execution_id"]
    assert calls[0] == dict(native_session_id="session-1", execution_id=first["execution_id"],
                            tool_name="mcp_magnus_procurement_mail_read", task_id="task-1", tool_call_id="tool-call-1",
                            turn_id="turn-1", api_request_id="api-1")
    assert "arguments" not in calls[0]
    with pytest.raises(RuntimeError, match="native_observation_execution_required"):
        dispatch(agent)
    assert len(calls) == 2


def test_blocked_tool_never_calls_factory_or_executor(make_agent, monkeypatch):
    factory, execute = Mock(), Mock()
    agent = make_agent(factory)
    engine(monkeypatch, lambda value: dispatch(value, execute, blocked="blocked by existing policy"))
    assert "blocked by existing policy" in agent.run_conversation("blocked")["final_response"]
    factory.assert_not_called()
    execute.assert_not_called()


def test_unconfigured_and_explicit_unrelated_context_retain_execution(make_agent, monkeypatch):
    engine(monkeypatch, dispatch)
    assert make_agent().run_conversation("ordinary")["final_response"] == {"unscoped": True}
    assert make_agent(lambda _meta: nullcontext()).run_conversation("unrelated")["final_response"] == {"unscoped": True}


@pytest.mark.parametrize("kind", ["raises", "invalid", "stale_session", "stale_execution", "uninstalled_scope"])
def test_invalid_configured_scope_refuses_before_tool(make_agent, monkeypatch, kind):
    from tools.mcp_observation import _Scope, mcp_observation_scope
    execute = Mock()

    def factory(meta):
        if kind == "raises":
            raise RuntimeError("PRIVATE resolver diagnostic")
        if kind == "invalid":
            return object()
        if kind == "uninstalled_scope":
            return nullcontext(_Scope("{}", "magnus", lambda _: None))
        return mcp_observation_scope(agent_run_id="approved-run", runtime_owner="hermes:magnus",
                                     native_session_id="foreign-session" if kind == "stale_session" else meta["native_session_id"],
                                     execution_id="foreign-execution" if kind == "stale_execution" else meta["execution_id"],
                                     intent_id="approved-intent", server_name="magnus", sink=lambda _: None)

    agent = make_agent(factory)
    engine(monkeypatch, lambda value: dispatch(value, execute))
    with pytest.raises(RuntimeError, match="native_observation_scope_invalid") as error:
        agent.run_conversation("invalid binding")
    assert "PRIVATE" not in str(error.value)
    execute.assert_not_called()


def test_session_rotation_cannot_relabel_old_binding(make_agent, monkeypatch):
    calls = []
    agent = make_agent(factory_for(calls, stale_session="session-1"))

    def rotate(value):
        first = dispatch(value)
        value.session_id = "rotated-session"
        with pytest.raises(RuntimeError, match="native_observation_scope_invalid"):
            dispatch(value)
        return first

    engine(monkeypatch, rotate)
    agent.run_conversation("rotate")
    assert [item["native_session_id"] for item in calls] == ["session-1", "rotated-session"]
    assert calls[0]["execution_id"] == calls[1]["execution_id"]


@pytest.mark.parametrize("interrupt", [False, True])
def test_nested_unconfigured_conversation_clears_parent_capture_then_restores_it(make_agent, monkeypatch, interrupt):
    calls = []
    parent = make_agent(factory_for(calls))
    child = make_agent(session="child-session")

    def run(value):
        if value is child:
            assert dispatch(child) == {"unscoped": True}
            if interrupt:
                raise InterruptedError("synthetic stop")
            return "child"

        def parent_tool(_args):
            from tools.mcp_observation import snapshot_mcp_observation
            before = snapshot_mcp_observation("magnus", "procurement_mail_read", {})
            if interrupt:
                with pytest.raises(InterruptedError):
                    child.run_conversation("nested")
            else:
                child.run_conversation("nested")
            after = snapshot_mcp_observation("magnus", "procurement_mail_read", {})
            assert json.loads(before.input_json)["execution_id"] == json.loads(after.input_json)["execution_id"]
            return "parent"

        return dispatch(parent, parent_tool)

    engine(monkeypatch, run)
    assert parent.run_conversation("outer")["final_response"] == "parent"
    from tools.mcp_observation import snapshot_mcp_observation
    assert snapshot_mcp_observation("magnus", "procurement_mail_read", {}) is None


def test_parallel_execution_frames_on_same_agent_are_separate_and_foreign_agent_is_denied(make_agent):
    from tools.mcp_observation_runtime import native_observation_execution
    from tools.thread_context import propagate_context_to_thread
    calls = []
    agent = make_agent(factory_for(calls))
    foreign_factory = Mock()
    foreign = make_agent(foreign_factory, session="other-session")
    barrier = threading.Barrier(2)

    def worker():
        with native_observation_execution(agent):
            barrier.wait(timeout=5)
            with pytest.raises(RuntimeError, match="native_observation_execution_required"):
                dispatch(foreign)
            return dispatch(agent)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(propagate_context_to_thread(worker)) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0]["execution_id"] != results[1]["execution_id"]
    foreign_factory.assert_not_called()
