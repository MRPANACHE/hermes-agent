"""Hermetic MCP SDK serialization, owned HTTP transport, and native typed reads.

All HTTP is synthetic through the SDK-owned httpx2 MockTransport. The in-memory
OAuth identity is synthetic; this test proves composition, not production admission.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from mcp.client.session import ClientSession
from tools import mcp_tool as core
from tools import mcp_tool_pax_recovery as recovery
from tools.mcp_oauth_manager import HermesMCPOAuthProvider
from tools.mcp_tool_transport import MCPServerTransportMixin


class Server(MCPServerTransportMixin):
    name = "brain"
    _http_rejection = {}
    _sampling = _elicitation = None
    _config = {"url": recovery.ENDPOINT, "protocol": "legacy"}


def _provider():
    import anyio
    provider = object.__new__(HermesMCPOAuthProvider)
    provider.context = NS(server_url=recovery.ENDPOINT,
        client_info=NS(client_id=recovery.CLIENT),
        current_tokens=NS(access_token="synthetic", scope="read", expires_in=3600),
        is_token_valid=lambda: True, oauth_metadata=None, storage=None)
    provider._initialized = True
    provider.context.lock = anyio.Semaphore(1, max_value=1)
    provider._hermes_server_name = "brain"
    provider._hermes_home = "synthetic-home"
    provider._add_auth_header = lambda request: request.headers.__setitem__("Authorization", "Bearer synthetic")
    return provider


@asynccontextmanager
async def _expected_terminal_transport(fault):
    """A denied writer can terminate the SDK taskgroup; never hide other errors."""
    try:
        yield
    except BaseExceptionGroup as group:
        def leaves(error):
            return [leaf for child in error.exceptions for leaf in leaves(child)] if isinstance(error, BaseExceptionGroup) else [error]
        errors = leaves(group)
        expected = "pax_native_read_transport_status" if isinstance(fault, int) else "pax_native_read_stopped"
        assert isinstance(fault, int) or fault == "concurrent"
        assert errors and all(isinstance(error, RuntimeError) and str(error) == expected for error in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, 401, 403, 302, 500, "discovery-reset", "queued-cancel", "concurrent"])
async def test_sdk_roundtrips_native_history_and_fact(monkeypatch, fault):
    assert core._ensure_mcp_sdk()
    httpx = core.sdk_httpx()
    wire = []
    initialized = asyncio.Event()
    started, release = asyncio.Event(), asyncio.Event()
    values = {
        "curation_history": {"items": [{"request_id": recovery.REQUEST, "pax_run": recovery.REQUEST,
            "source_id": recovery.SOURCE, "curator_client_id": recovery.CLIENT,
            "decisions": [{"action":"add", "old_fact_ids":[], "new_fact_id":7,
                "visibility":"private", "reason":"synthetic", "source_refs":[]}]}], "next_cursor":None},
        "list_facts": {"facts":[{"id":7,"source_id":recovery.SOURCE,"source_session":recovery.REQUEST}]},
    }
    async def endpoint(request):
        wire.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name":"synthetic","version":"1"}}
        elif method == "notifications/initialized":
            initialized.set()
            return httpx.Response(202)
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                for name in values]}
        elif method == "tools/call":
            if isinstance(fault, int):
                return httpx.Response(fault, headers={"location": "https://forbidden.invalid"})
            if fault == "concurrent":
                started.set()
                await release.wait()
            params = body["params"]
            result = {"content":[{"type":"text","text":json.dumps(values[params["name"]])}],"isError":False}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"jsonrpc":"2.0","id":body["id"],"result":result},
                              headers={"content-type":"application/json"})
    server = Server()
    provider = _provider()
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(endpoint))
    monkeypatch.setattr(core, "_MCP_NEW_HTTP", True)
    monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", lambda: NS(invalidate_if_disk_changed=lambda *a, **k: asyncio.sleep(0, result=False)))
    # Real SDK streamable_http_client and ClientSession; only the socket boundary is synthetic.
    async with _expected_terminal_transport(fault), server._streamable_http_transport(recovery.ENDPOINT, {}, 2, True, None, provider, False, set()) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await asyncio.wait_for(initialized.wait(), timeout=2)
            # The admitted native connection has already completed discovery.
            # MCP validates call results using this real list_tools cache; the
            # recovery must never trigger discovery itself under frozen auth.
            await session.list_tools()
            server.session = session
            server._rpc_lock = asyncio.Lock()
            server._reconnecting = False
            server._inflight_tasks = set()
            reader = recovery.PaxRecoveryReads(server, provider, lambda: server)
            async def forbid_reload(*args, **kwargs):
                raise AssertionError("Recovery cannot reload/refresh OAuth")
            monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", lambda: NS(invalidate_if_disk_changed=forbid_reload))
            if fault == "discovery-reset":
                session._tool_output_schemas.clear()
                with pytest.raises(recovery.RecoveryReadError, match="discovery_not_current"):
                    await reader.exchange({"op": "history"})
            elif fault == "queued-cancel":
                await server._rpc_lock.acquire()
                try:
                    task = asyncio.create_task(reader.exchange({"op": "history"}))
                    await asyncio.sleep(0)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert reader.stopped
                finally:
                    server._rpc_lock.release()
                await asyncio.sleep(0)
            elif fault == "concurrent":
                task = asyncio.create_task(reader.exchange({"op": "history"}))
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    with pytest.raises(recovery.RecoveryReadError, match="concurrent_exchange"):
                        await reader.exchange({"op": "history"})
                    release.set()
                    with pytest.raises((recovery.RecoveryReadError, RuntimeError)):
                        await asyncio.wait_for(task, timeout=2)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            elif fault is not None:
                with pytest.raises((recovery.RecoveryReadError, RuntimeError)):
                    await asyncio.wait_for(reader.exchange({"op": "history"}), timeout=2)
            else:
                for _ in range(2):
                    history = await reader.exchange({"op":"history"})
                    assert history["items"][0]["request_id"] == recovery.REQUEST
                    fact = await reader.exchange({"op":"fact", "index":0})
                    assert fact["facts"][0]["id"] == 7
            if fault is not None:
                assert reader.stopped
                with pytest.raises(recovery.RecoveryReadError, match="pax_native_read_stopped"):
                    await reader.exchange({"op": "history"})
            await reader.close()
            assert getattr(provider, "_pax_frozen_read", None) is None
    await reader.close()
    assert getattr(provider, "_pax_frozen_read", None) is None
    assert not reader.active
    if fault is not None:
        assert reader.stopped
    posts = [json.loads(req.content) for req in wire if req.method == "POST"]
    calls = [item for item in posts if item.get("method") == "tools/call"]
    expected = ["curation_history", "list_facts"] * 2 if fault is None else (
        [] if fault in {"discovery-reset", "queued-cancel"} else ["curation_history"])
    assert [c["params"]["name"] for c in calls] == expected
    assert len([item for item in posts if item.get("method") == "tools/list"]) == 1
    assert all(c["params"]["_meta"]["io.mrpanache/oos-native-session"]["session_id"] == recovery.NATIVE_SESSION for c in calls)


@pytest.mark.asyncio
async def test_late_native_request_is_denied_before_oauth_fallback():
    provider = _provider()
    req = NS(content=json.dumps({"params":{"_meta":{"io.mrpanache/oos-native-session":{"session_id":recovery.NATIVE_SESSION}}}}).encode())
    with patch("tools.mcp_oauth_manager.get_manager", side_effect=AssertionError("OAuth reload forbidden")):
        with pytest.raises(RuntimeError, match="no_active_host"):
            await provider.async_auth_flow(req).__anext__()
