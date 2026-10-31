"""Synthetic owner -> real SDK/HTTP/auth -> socket -> unchanged sealed queue/SQLite.

No live server, OAuth storage, credentials, source mutation or recovery invocation.
Only the test binding/HTTP socket boundary is replaced. Product binding is not
activated: its production release seal/job preflight remains a release gate.
"""
import asyncio
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import anyio
import pytest
from mcp.client.session import ClientSession

from agent import pax_recovery_owner as owner
from agent.turn_facade_lease import DurableTurnLease
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools import mcp_tool as core
from tools import mcp_tool_pax_recovery as recovery
from tools.mcp_oauth_manager import HermesMCPOAuthProvider, MCPOAuthManager, _ProviderEntry
from tools.mcp_tool_scope import _server_key
from tools.mcp_tool_transport import MCPServerTransportMixin

HERE = Path(__file__).parent


class SyntheticServer(MCPServerTransportMixin):
    name = "brain"
    _sampling = _elicitation = None
    _config = {"url": recovery.ENDPOINT, "protocol": "legacy"}
    _auth_type = "oauth"

    def __init__(self):
        self._shutdown_event = asyncio.Event()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "success", "wrong-curator", "fact-mismatch", "history-changed", "conflict",
    "changed-raw", "before-audit", "after-audit", "before-jobwrite", "after-jobwrite",
    "owner-interrupt", "lease-loss", "timeout",
])
async def test_combined_owner_sdk_consumer(tmp_path, monkeypatch, scenario):
    home = tmp_path / "pax"
    home.mkdir()
    (home / "scenario.txt").write_text(scenario)
    token = set_hermes_home_override(home)
    monkeypatch.setattr(recovery, "HOME", home)
    monkeypatch.setattr(owner, "_BINDING", HERE / "fixture.mjs")
    if scenario == "timeout":
        monkeypatch.setattr(owner, "_ACTION_TIMEOUT", 1.5)
    db = SessionDB(home / "state.db")
    session_id, holder = "synthetic-current-owner", "synthetic-holder"
    db.create_session(session_id, "cli")
    assert db.acquire_session_turn_lease(session_id, holder, wait_seconds=0)
    agent = NS(session_id=session_id, _session_db=db, _active_session_turn_lease_holder=holder,
               _interrupt_requested=False, _pending_redirect_lock=threading.Lock())
    lease = DurableTurnLease(agent, db, session_id, holder)
    lease.turn_active = True
    core._ensure_mcp_sdk()
    httpx = core.sdk_httpx()
    assert httpx is not None
    initialized = asyncio.Event()
    wire = []
    provider = object.__new__(HermesMCPOAuthProvider)
    provider.context = NS(server_url=recovery.ENDPOINT, client_info=NS(client_id=recovery.CLIENT),
        current_tokens=NS(access_token="synthetic-only", scope="read"), is_token_valid=lambda: True,
        lock=anyio.Semaphore(1, max_value=1), oauth_metadata=None, storage=None)
    provider._initialized = True
    provider._hermes_server_name = "brain"
    provider._hermes_home = str(home)
    provider._add_auth_header = lambda request: request.headers.__setitem__("Authorization", "Bearer synthetic-only")
    manager = MCPOAuthManager()
    manager._entries[(str(home), "brain")] = _ProviderEntry(recovery.ENDPOINT, None, provider)
    async def handshake_only(*args, **kwargs):
        return False
    monkeypatch.setattr(manager, "invalidate_if_disk_changed", handshake_only)
    monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", lambda: manager)
    server = SyntheticServer()
    server._http_rejection = {}
    server._rpc_lock = asyncio.Lock()
    server._reconnecting = False
    server._inflight_tasks = set()
    monkeypatch.setattr(core, "_mcp_loop", asyncio.get_running_loop())
    key = _server_key("brain")
    monkeypatch.setitem(core._servers, key, server)

    async def endpoint(request):
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        wire.append(body)
        method = body["method"]
        if method == "initialize":
            value = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "synthetic", "version": "1"}}
        elif method == "notifications/initialized":
            initialized.set()
            return httpx.Response(202)
        elif method == "tools/list":
            value = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                               for name in ("curation_history", "list_facts")]}
        else:
            assert method == "tools/call"
            if scenario == "timeout":
                # A real in-flight SDK POST, cancelled by owner expiry/cleanup.
                await asyncio.Event().wait()
            assert body["params"]["_meta"] == {"io.mrpanache/oos-native-session": {"session_id": recovery.NATIVE_SESSION}}
            fixture = json.loads((home / "fixture-ready.json").read_text())
            if body["params"]["name"] == "curation_history":
                row = copy.deepcopy(fixture["history"])
                if scenario == "wrong-curator":
                    row["curator_client_id"] = "synthetic-other-reader"
                if scenario == "history-changed" and len([b for b in wire if b["method"] == "tools/call"]) > 2:
                    row["created_at"] = "2026-10-28T20:00:00.000Z"
                data = {"items": [row], "next_cursor": None}
            else:
                row = fixture["fact"]
                if scenario == "fact-mismatch":
                    row["source_session"] = "synthetic-other"
                data = {"facts": [row]}
            if scenario == "owner-interrupt":
                agent._interrupt_requested = True
            if scenario == "lease-loss":
                db.release_session_turn_lease(session_id, holder)
            value = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": value},
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(endpoint))
    result, failure = None, None
    try:
        async with server._streamable_http_transport(recovery.ENDPOINT, {}, 2, True, None, provider, False, set()) as streams:
            async with ClientSession(streams[0], streams[1]) as sdk_session:
                await sdk_session.initialize()
                await asyncio.wait_for(initialized.wait(), timeout=2)
                await sdk_session.list_tools()
                server.session = sdk_session
                async def forbid_reload(*args, **kwargs):
                    raise AssertionError("Recovery cannot load/refresh auth")
                monkeypatch.setattr(manager, "invalidate_if_disk_changed", forbid_reload)
                try:
                    result = await asyncio.wait_for(asyncio.to_thread(lease.run_pax_recovery_owner_action), timeout=20)
                except recovery.RecoveryReadError as exc:
                    failure = str(exc)
                assert provider._pax_frozen_read is None
    finally:
        lease.release()
        db.close()
        reset_hermes_home_override(token)
    fixture = json.loads((home / "fixture-ready.json").read_text())
    job = json.loads(Path(fixture["path"]).read_text())
    calls = [b for b in wire if b["method"] == "tools/call"]
    assert len(calls) <= 4
    assert agent._pax_recovery_consumed
    assert not any(t.name == "pax-recovery-owned-read" and t.is_alive() for t in threading.enumerate())
    assert len([b for b in wire if b["method"] == "tools/list"]) == 1  # setup only
    if scenario == "success":
        assert result["ok"] is True
        assert result["events"] == 1
        assert job["status"] == "completed"
        assert "curation_runtime" not in job
        assert result["result"]["historical_runtime"]["status"] == "unknown"
        assert [b["params"]["name"] for b in calls] == ["curation_history", "list_facts"] * 2
    elif scenario == "after-jobwrite":
        assert result["ok"] is False
        assert result["events"] == 1
        assert job["status"] == "completed"  # Never restore a failed JSON after a possible effect.
    else:
        assert job["status"] == "failed"
        assert failure is not None or result["ok"] is False
        if scenario != "changed-raw":
            assert Path(fixture["path"]).read_text() == fixture["raw"]
    print(json.dumps({"scenario": scenario, "synthetic_only": True, "wire_calls": len(calls),
                      "job_status": job["status"], "host_failure": failure,
                      "consumer_ok": None if result is None else result["ok"]}))
