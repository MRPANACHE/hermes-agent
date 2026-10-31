"""Synthetic wire, real SDK and native run/serve/reconnect consumers (review R1).

No live profile, tokens, sockets or source reads. The first native connection is
admitted normally; every later config/auth/discovery/reconnect operation is fatal.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from mcp.shared.exceptions import MCPError

from tools import mcp_tool as core
from tools import mcp_tool_pax_recovery as recovery
from tools.mcp_oauth_manager import HermesMCPOAuthProvider

_PENDING_FAULTS = {"pending-cancel", "pending-close", "concurrent"}


def _provider():
    import anyio

    provider = object.__new__(HermesMCPOAuthProvider)
    provider.context = NS(server_url=recovery.ENDPOINT, client_info=NS(client_id=recovery.CLIENT),
        current_tokens=NS(access_token="synthetic", scope="read"), is_token_valid=lambda: True,
        lock=anyio.Semaphore(1, max_value=1), oauth_metadata=None, storage=None)
    provider._initialized = True
    provider._hermes_server_name = "brain"
    provider._hermes_home = "synthetic-only"
    provider._add_auth_header = lambda request: request.headers.__setitem__("Authorization", "Bearer synthetic")
    return provider


class NativeServer(core.MCPServerTask):
    def __init__(self, provider, initialized):
        super().__init__("brain")
        self.provider = provider
        self.initialized = initialized
        self.connects = self.discoveries = 0

    async def _discover_tools(self):
        self.discoveries += 1
        assert self.discoveries == 1, "post-admission discovery forbidden"
        await self.initialized.wait()
        await self.session.list_tools()

    def _build_oauth_auth(self, url, config):
        assert self.connects == 1, "post-admission auth build forbidden"
        return self.provider

    async def _run_http(self, config):
        self.connects += 1
        assert self.connects == 1, "post-admission reconnect forbidden"
        return await super()._run_http(config)


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaves(child)]
    return [error]


def _history():
    return {"items": [{"request_id": recovery.REQUEST, "pax_run": recovery.REQUEST,
        "source_id": recovery.SOURCE, "curator_client_id": recovery.CLIENT,
        "decisions": [{"action": "add", "old_fact_ids": [], "new_fact_id": 7,
                       "visibility": "private", "reason": "synthetic", "source_refs": []}]}], "next_cursor": None}


def _endpoint(httpx, wire, initialized, pending, fault):
    async def endpoint(request):
        assert request.method == "POST", "unexpected wire method"
        body = json.loads(request.content)
        wire.append(body["method"])
        if body["method"] == "initialize":
            value = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "synthetic", "version": "1"}}
        elif body["method"] == "notifications/initialized":
            initialized.set()
            return httpx.Response(202)
        elif body["method"] == "tools/list":
            value = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                               for name in ("curation_history", "list_facts")]}
        else:
            assert body["method"] == "tools/call"
            if fault in _PENDING_FAULTS:
                pending.set()
                await asyncio.Event().wait()
            if isinstance(fault, int):
                return httpx.Response(fault, headers={"location": "https://forbidden.invalid"})
            assert fault is None
            value = {"content": [{"type": "text", "text": json.dumps(_history())}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": value},
                              headers={"content-type": "application/json"})
    return endpoint


async def _pending_fault(reader, pending, fault):
    read = asyncio.create_task(reader.exchange({"op": "history"}))
    try:
        await asyncio.wait_for(pending.wait(), timeout=3)
        if fault == "pending-close":
            await reader.close()
        elif fault == "concurrent":
            with pytest.raises(recovery.RecoveryReadError, match="concurrent_exchange"):
                await reader.exchange({"op": "history"})
        else:
            read.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError, MCPError)) as caught:
            await read
        if isinstance(caught.value, MCPError):
            assert str(caught.value) == "Connection closed"
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def _exercise_fault(reader, server, provider, pending, fault):
    if fault is None:
        assert await reader.exchange({"op": "history"}) == _history()
    elif fault == "late-send":
        # A native request queued before guard cleanup must not fall into ordinary auth.
        with pytest.raises(MCPError, match="^Connection closed$"):
            await server.session.call_tool("curation_history", {},
                meta={"io.mrpanache/oos-native-session": {"session_id": recovery.NATIVE_SESSION}})
    elif fault == "queued-cancel":
        await server._rpc_lock.acquire()
        read = asyncio.create_task(reader.exchange({"op": "history"}))
        try:
            await asyncio.sleep(0)
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        finally:
            server._rpc_lock.release()
    elif fault in _PENDING_FAULTS:
        await _pending_fault(reader, pending, fault)
    else:
        if fault == "owner-lost":
            reader.owner_check = lambda: recovery.require(False, "owner_inactive")
        if fault == "auth-owner-lost":
            provider._add_auth_header = lambda request: recovery.require(False, "owner_inactive")
        if fault == "discovery-reset":
            server.session._tool_output_schemas.clear()
        with pytest.raises((recovery.RecoveryReadError, RuntimeError, MCPError)) as caught:
            await reader.exchange({"op": "history"})
        if isinstance(caught.value, MCPError):
            assert str(caught.value) == "Connection closed"


def _forbidden(*args, **kwargs):
    pytest.fail("post-admission config/auth/reconnect hook forbidden")


async def _unchanged(*args, **kwargs):
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, 401, 403, 302, 500, "late-send", "queued-cancel",
    "pending-cancel", "pending-close", "concurrent", "owner-lost", "auth-owner-lost", "discovery-reset"])
async def test_recovery_failure_stops_real_native_run_before_rebuild(monkeypatch, fault):
    assert core._ensure_mcp_sdk()
    httpx = core.sdk_httpx()
    initialized, pending = asyncio.Event(), asyncio.Event()
    provider = _provider()
    server = NativeServer(provider, initialized)
    wire = []
    endpoint = _endpoint(httpx, wire, initialized, pending, fault)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(endpoint))
    monkeypatch.setattr("tools.mcp_tool_transport._connect_inputs", lambda *a: ([recovery.ENDPOINT, {}], set()))
    monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", lambda: NS(invalidate_if_disk_changed=_unchanged))
    monkeypatch.setattr("tools.mcp_tool_config._load_mcp_config", _forbidden)
    monkeypatch.setattr("tools.mcp_tool_discovery._install_owner_secret_scope", _forbidden)
    monkeypatch.setattr(server, "_park", _forbidden)
    monkeypatch.setattr(server, "_backoff_sleep", _forbidden)
    run = asyncio.create_task(server.run({"url": recovery.ENDPOINT, "auth": "oauth", "protocol": "legacy"}))
    reader = None
    try:
        await asyncio.wait_for(server._ready.wait(), timeout=3)
        assert server.session is not None
        monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", _forbidden)
        reader = recovery.PaxRecoveryReads(server, provider, lambda: server)
        await _exercise_fault(reader, server, provider, pending, fault)
        # Failures latch BEFORE cleanup clears the temporary provider guard.
        assert server._shutdown_event.is_set() == (fault is not None)
        await reader.close()
        assert getattr(provider, "_pax_frozen_read", None) is None
        if fault is None:
            assert not server._shutdown_event.is_set()
            assert server._reconnect_or_reraise_group(ExceptionGroup("ordinary", [ConnectionError()])) == "reconnect"
            server._shutdown_event.set()  # Explicit test teardown, not a recovery failure.
        result = await asyncio.wait_for(asyncio.gather(run, return_exceptions=True), timeout=3)
        if isinstance(result[0], BaseException):
            errors = _leaves(result[0])
            assert all(isinstance(e, (RuntimeError, asyncio.CancelledError)) for e in errors)
            assert all(isinstance(e, asyncio.CancelledError) or str(e).startswith("pax_native_read_") for e in errors)
        assert server.session is None
        assert provider._pax_native_stop is None
        assert server.connects == server.discoveries == 1
        assert wire[:3] == ["initialize", "notifications/initialized", "tools/list"]
        expected_calls = fault is None or isinstance(fault, int) or fault in _PENDING_FAULTS
        assert wire[3:] == (["tools/call"] if expected_calls else [])
        assert not reader.active
        assert not server._inflight_tasks
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        if reader is not None:
            await reader.close()
