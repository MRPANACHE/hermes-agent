"""Synthetic MCP transport; actual synchronous handler and MCP loop thread."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest


@pytest.fixture
def capture(monkeypatch):
    import tools.mcp_tool as mcp
    from tools.mcp_observation import mcp_observation_scope
    from mcp.types import CallToolResult, TextContent

    calls, observations = [], []
    result = CallToolResult(content=[TextContent(type="text", text="original text")])

    async def call_tool(name, arguments):
        calls.append((name, json.loads(json.dumps(arguments)), threading.get_ident()))
        return state.result

    async def sink(value):
        observations.append(value)

    mcp._ensure_mcp_loop()

    async def server():
        return SimpleNamespace(session=SimpleNamespace(call_tool=call_tool), _rpc_lock=asyncio.Lock())

    connected = mcp._run_on_mcp_loop(server)
    monkeypatch.setitem(mcp._servers, "magnus_capture", connected)
    monkeypatch.setitem(mcp._servers, "other_capture", connected)
    monkeypatch.setattr(mcp, "_server_error_counts", {})
    monkeypatch.setattr(mcp, "_server_breaker_opened_at", {})
    auth = Mock(return_value=None)
    session = Mock(return_value=None)
    monkeypatch.setattr(mcp, "_handle_auth_error_and_retry", auth)
    monkeypatch.setattr(mcp, "_handle_session_expired_and_retry", session)
    binding = dict(agent_run_id="run-1", runtime_owner="hermes:magnus", native_session_id="session-1",
                   execution_id="execution-1", intent_id="intent-1", server_name="magnus_capture", sink=sink)
    state = SimpleNamespace(mcp=mcp, scope=mcp_observation_scope, binding=binding, result=result,
                            calls=calls, observations=observations, server=connected, auth=auth, recovery=session)
    state.handler = mcp._make_tool_handler("magnus_capture", "procurement_mail_document_read", 10,
                                         image_content_mode="multimodal")
    try:
        yield state
    finally:
        mcp._stop_mcp_loop()


@pytest.mark.parametrize("is_error", [False, True])
def test_original_wire_result_is_captured_before_display(capture, is_error):
    from mcp.types import CallToolResult, ImageContent, TextContent

    capture.result = CallToolResult(
        content=[TextContent(type="text", text="evidence" * 5000),
                 ImageContent(type="image", mimeType="image/png", data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")],
        structuredContent={"timing": "original-outcome", "nested": {"units": 4}},
        isError=is_error, _meta={"mcp.private/trace": "original metadata"},
    )
    expected = capture.result.model_dump(mode="json", by_alias=True)
    args = {"source_sha256": "a" * 64}
    with capture.scope(**capture.binding):
        output = capture.handler(args)
    assert output
    [observation] = capture.observations
    assert set(observation) == {"agent_run_id", "runtime_owner", "native_session_id", "execution_id",
                                "invocation_id", "tool_name", "intent_id", "arguments", "result"}
    assert observation["result"] == expected
    assert observation["arguments"] == args
    assert observation["tool_name"] == "procurement_mail_document_read"
    assert str(UUID(observation["invocation_id"])) == observation["invocation_id"]
    assert capture.calls[0][2] != threading.get_ident()


def test_arguments_are_copied_before_transport_and_sink_cannot_mutate_display(capture):
    async def mutate(name, arguments):
        arguments["nested"]["value"] = "transport mutation"
        capture.calls.append(name)
        return capture.result

    async def sink(value):
        capture.observations.append(json.loads(json.dumps(value)))
        value["result"]["content"][0]["text"] = "sink mutation"
        value["arguments"]["nested"]["value"] = "sink mutation"

    capture.server.session.call_tool = mutate
    args = {"nested": {"value": "original"}}
    with capture.scope(**{**capture.binding, "sink": sink}):
        output = capture.handler(args)
    assert json.loads(output)["result"] == "original text"
    assert capture.observations[0]["arguments"] == {"nested": {"value": "original"}}
    assert args == {"nested": {"value": "original"}}


def test_overlapping_thread_scopes_keep_identity_and_reset(capture):
    from tools.thread_context import propagate_context_to_thread

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = []
        for number in [1, 2]:
            binding = {**capture.binding, "agent_run_id": f"run-{number}", "intent_id": f"intent-{number}"}
            with capture.scope(**binding):
                jobs.append(executor.submit(propagate_context_to_thread(capture.handler), {"number": number}))
        assert all(job.result(timeout=10) for job in jobs)
        executor.submit(propagate_context_to_thread(capture.handler), {}).result(timeout=10)
    assert len(capture.observations) == 2
    assert {(o["agent_run_id"], o["intent_id"], o["arguments"]["number"]) for o in capture.observations} == {
        ("run-1", "intent-1", 1), ("run-2", "intent-2", 2)}
    assert len({o["invocation_id"] for o in capture.observations}) == 2


@pytest.mark.parametrize("message", ["401 Unauthorized SECRET", "session not found 404 SECRET"])
def test_sink_failure_never_enters_execution_recovery(capture, message):
    async def reject(value):
        capture.observations.append(value)
        raise RuntimeError(message)

    with capture.scope(**{**capture.binding, "sink": reject}):
        result = json.loads(capture.handler({}))
    assert result["error"] == "mcp_observation_unconfirmed"
    assert result["execution_returned"] is True
    assert result["observation_confirmed"] is False
    assert result["invocation_id"] == capture.observations[0]["invocation_id"]
    assert result["next_action"] == "retry_observation_only_do_not_repeat_tool"
    assert "SECRET" not in json.dumps(result)
    assert len(capture.calls) == 1
    capture.auth.assert_not_called()
    capture.recovery.assert_not_called()


@pytest.mark.parametrize("result", [SimpleNamespace(content=[], isError=False), "oversized"])
def test_unknown_or_oversized_original_result_is_visible_failure(capture, result):
    from mcp.types import CallToolResult, TextContent

    capture.result = CallToolResult(content=[TextContent(type="text", text="x" * (8 * 1024 * 1024))]) if result == "oversized" else result
    with capture.scope(**capture.binding):
        output = json.loads(capture.handler({}))
    assert output["error"] == "mcp_observation_unconfirmed"
    assert output["execution_returned"] is True
    assert len(capture.calls) == 1
    assert capture.observations == []
    capture.auth.assert_not_called()
    capture.recovery.assert_not_called()


def test_unscoped_and_foreign_server_calls_are_unchanged(capture):
    assert json.loads(capture.handler({}))["result"] == "original text"
    other = capture.mcp._make_tool_handler("other_capture", "read", 10)
    with capture.scope(**capture.binding):
        assert json.loads(other({}))["result"] == "original text"
    assert capture.observations == []


def test_invalid_arguments_refuse_before_native_call(capture):
    with capture.scope(**capture.binding):
        output = json.loads(capture.handler({"not_json": object()}))
    assert output["error"] == "mcp_observation_input_invalid"
    assert output["execution_returned"] is False
    assert capture.calls == []


@pytest.mark.parametrize("interrupted", [False, True])
def test_pending_sink_timeout_or_interrupt_reports_returned_execution(capture, monkeypatch, interrupted):
    started = threading.Event()
    cancelled = threading.Event()

    async def waiting_sink(value):
        capture.observations.append(value)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if interrupted:
        monkeypatch.setattr("tools.interrupt.is_interrupted", started.is_set)
    handler = capture.mcp._make_tool_handler("magnus_capture", "procurement_mail_document_read", 2)
    with capture.scope(**{**capture.binding, "sink": waiting_sink}):
        result = json.loads(handler({}))
    assert started.is_set()
    assert cancelled.wait(5), "cancelled sink task must finish before teardown"
    assert result["error"] == "mcp_observation_unconfirmed"
    assert result["execution_returned"] is True
    assert result["observation_confirmed"] is False
    assert result["invocation_id"] == capture.observations[0]["invocation_id"]
    assert len(capture.calls) == 1
    capture.auth.assert_not_called()
    capture.recovery.assert_not_called()


@pytest.mark.parametrize("integer", [2**53, -(2**53)])
def test_integer_outside_javascript_exact_range_refuses_before_call(capture, integer):
    with capture.scope(**capture.binding):
        output = json.loads(capture.handler({"integer": integer}))
    assert output["error"] == "mcp_observation_input_invalid"
    assert output["execution_returned"] is False
    assert capture.calls == []


def test_original_unsafe_integer_result_is_unconfirmed_and_safe_boundaries_survive(capture):
    from mcp.types import CallToolResult, TextContent

    capture.result = CallToolResult(content=[TextContent(type="text", text="integer result")],
                                    structuredContent={"integer": 2**53 + 1})
    with capture.scope(**capture.binding):
        output = json.loads(capture.handler({}))
    assert output["error"] == "mcp_observation_unconfirmed"
    assert output["execution_returned"] is True
    assert capture.observations == []
    assert len(capture.calls) == 1
    capture.result = CallToolResult(content=[], structuredContent={"integer": 2**53 - 1})
    with capture.scope(**capture.binding):
        capture.handler({"integer": -(2**53 - 1)})
    assert capture.observations[0]["arguments"]["integer"] == -(2**53 - 1)
    assert capture.observations[0]["result"]["structuredContent"]["integer"] == 2**53 - 1
