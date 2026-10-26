"""Runtime session routing is request metadata, independent of model arguments."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import ClientSession, types

from tools import mcp_tool, mcp_tool_handlers, mcp_tool_loop
from tools.registry import ToolRegistry

KEY = "io.mrpanache/oos-native-session"


@pytest.fixture
def shared_connection(monkeypatch):
    loop = asyncio.new_event_loop()
    requests = []
    session = MagicMock(spec=ClientSession)
    session._call_tool_adapter = object()
    session.call_tool = MethodType(ClientSession.call_tool, session)
    session.validate_tool_result = AsyncMock()

    async def send_request(request, *args, **kwargs):
        requests.append(request.model_dump(by_alias=True, exclude_none=True))
        return types.CallToolResult(content=[types.TextContent(type="text", text="received")])

    session.send_request = send_request
    server = SimpleNamespace(session=session, _rpc_lock=asyncio.Lock())
    monkeypatch.setitem(mcp_tool._servers, "native-session-test", server)
    monkeypatch.setattr(mcp_tool_loop, "_run_on_mcp_loop", lambda factory, **kwargs: loop.run_until_complete(factory()))
    registry = ToolRegistry()
    registry.register(
        name="native_session_test", toolset="mcp_native_session_test",
        schema={"name": "native_session_test", "parameters": {"type": "object"}},
        handler=mcp_tool_handlers._make_tool_handler("native-session-test", "ace_context", 30),
    )
    try:
        yield registry, requests
    finally:
        loop.close()


def test_shared_mcp_session_interleaves_native_a_b_a_without_model_override(shared_connection):
    registry, requests = shared_connection
    args = {"session_id": "logical-ace-session", "task_id": "existing-task",
            "_meta": {KEY: {"session_id": "model-forged"}}}
    for native_id in ["native-A", "native-B", "native-A"]:
        result = registry.dispatch("native_session_test", args, session_id=native_id)
        assert "received" in result

    assert [request["params"].get("_meta") for request in requests] == [
        {KEY: {"session_id": native_id}} for native_id in ["native-A", "native-B", "native-A"]
    ]
    assert all(request["params"]["arguments"] == args for request in requests)
    assert args["_meta"][KEY]["session_id"] == "model-forged"


def test_model_and_process_session_ids_cannot_create_native_metadata(shared_connection, monkeypatch):
    registry, requests = shared_connection
    monkeypatch.setenv("HERMES_SESSION_ID", "process-forged")
    args = {"session_id": "model-logical", "_meta": {KEY: {"session_id": "model-forged"}}}
    registry.dispatch("native_session_test", args)
    assert "_meta" not in requests[0]["params"]
