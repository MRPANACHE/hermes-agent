"""Unregistered, dedicated native-host read composition. Preparation only.

This is runtime-owned code, not a plugin importing a private handler to evade
PluginContext.call_mcp. No model tool/command, discovery, auth load or retry.
The future release owner must admit its host call site separately.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

RECOVERY = "5e5c7051b1058e0f9b03d07afac9ec0da593adb8aee4db0135432c4dddcaa192"
REQUEST = "pax-curation:" + RECOVERY
NATIVE_SESSION = "20261027_214817_10339b"
SOURCE = "business-pb"
CLIENT = "gbrain_cl_208108dbcf6e7d84a7c1d2cffebf74606d23f086467acafbcb62370030152287"
ENDPOINT = "http://127.0.0.1:3131/mcp"
HOME = Path("/home/david/.hermes/profiles/pax")
MAX_BYTES = 256 * 1024
TIMEOUT = 10.0


class RecoveryReadError(RuntimeError):
    pass


def marked_request(request):
    """Detect only this native host's fixed metadata, including late queued sends."""
    try:
        body = json.loads(request.content)
        return body.get("params", {}).get("_meta", {}).get("io.mrpanache/oos-native-session", {}).get("session_id") == NATIVE_SESSION
    except (ValueError, TypeError, AttributeError):
        return False


def require(condition, reason):
    if not condition:
        raise RecoveryReadError("pax_native_read_" + reason)


def payload(result):
    """No rendering, spill, head/tail truncation, captures or best-effort parsing."""
    from tools.mcp_tool_common import mcp_field
    require(mcp_field(result, "is_error", "isError", False) is False, "remote_error")
    require(getattr(result, "result_type", "complete") == "complete", "result_incomplete")
    blocks = getattr(result, "content", None)
    structured = mcp_field(result, "structured_content", "structuredContent")
    require(isinstance(blocks, list), "content_missing")
    require(len(blocks) <= 1, "content_ambiguous")
    text = None
    if blocks:
        block = blocks[0]
        require(getattr(block, "type", None) == "text", "content_invalid")
        text = getattr(block, "text", None)
        require(isinstance(text, str) and len(text.encode()) <= MAX_BYTES, "content_overflow")
        try:
            text = json.loads(text)
        except (ValueError, TypeError):
            raise RecoveryReadError("pax_native_read_content_invalid") from None
    value = structured if structured is not None else text
    require(isinstance(value, dict) and "error" not in value, "content_invalid")
    require(text is None or structured is None or text == structured, "content_conflict")
    require(len(json.dumps(value).encode()) <= MAX_BYTES, "content_overflow")
    return value


class FrozenPaxAuth:
    """Native provider's existing in-memory identity, one exact resource POST.

    Installed only while the native RPC lock is held. The HTTP request hook runs
    for redirects too; it prevents a second POST or any auth endpoint/refresh.
    Tokens remain inside the existing provider and are never returned/exported.
    """
    def __init__(self, provider, tool, arguments):
        self.provider = provider
        self.tool = tool
        self.arguments = arguments
        self.sent = False
        self.failed = False
        self.check_identity()

    def check_identity(self):
        ctx = self.provider.context
        require(str(ctx.server_url) == ENDPOINT, "endpoint_mismatch")
        require(getattr(ctx.client_info, "client_id", None) == CLIENT, "unauthorized_reader")
        require(ctx.current_tokens is not None and ctx.is_token_valid(), "auth_not_current")
        require("read" in (getattr(ctx.current_tokens, "scope", "") or "").split(), "read_scope_missing")

    async def guard_request(self, request):
        try:
            self.check_identity()
            require(not self.sent and not self.failed, "transport_replay")
            require(str(request.url) == ENDPOINT and request.method == "POST", "endpoint_mismatch")
            body = json.loads(request.content)
            params = body.get("params", {})
            require(body.get("method") == "tools/call" and params.get("name") == self.tool
                    and params.get("arguments") == self.arguments, "transport_request_mismatch")
            require(params.get("_meta") == {"io.mrpanache/oos-native-session": {"session_id": NATIVE_SESSION}},
                    "native_session_mismatch")
            require(set(params) == {"name", "arguments", "_meta"}, "transport_request_mismatch")
            self.sent = True
            request.extensions["pax_recovery_guard"] = self
        except BaseException:
            self.failed = True
            raise

    async def auth_flow(self, request):
        self.check_identity()
        require(not self.failed, "transport_failed")
        # Supported SDK provider method; no token extraction and no SDK auth ladder.
        self.provider._add_auth_header(request)
        response = yield request
        require(200 <= response.status_code < 300, "transport_status")


class PaxRecoveryReads:
    """Typed history/fact exchange for the fixed consumer, at most two fresh rounds."""
    def __init__(self, server, provider, connected):
        self.server = server
        self.session = server.session
        self.provider = provider
        self.connected = connected
        self.history = None
        self.rounds = 0
        self.pending = set()
        self.stopped = False

    async def _read(self, tool, arguments):
        from tools.mcp_tool_handlers import _call_tool_racing_stdio_death, _track_inflight_rpc
        require(self.connected() is self.server and self.server.session is self.session, "connection_changed")
        async with self.server._rpc_lock:
            require(self.server.session is self.session and not getattr(self.server, "_reconnecting", False), "connection_changed")
            require(getattr(self.provider, "_pax_frozen_read", None) is None, "concurrent_read")
            guard = FrozenPaxAuth(self.provider, tool, arguments)
            self.provider._pax_frozen_read = guard
            try:
                async with _track_inflight_rpc(self.server, "brain", tool, retry_safe=False):
                    result = await asyncio.wait_for(_call_tool_racing_stdio_death(
                        self.server, "brain", tool, arguments, session_id=NATIVE_SESSION), TIMEOUT)
                require(guard.sent and not guard.failed, "unguarded_transport")
                guard.check_identity()
                require(self.connected() is self.server and self.server.session is self.session, "connection_changed")
                return payload(result)
            finally:
                self.provider._pax_frozen_read = None

    async def exchange(self, message):
        try:
            require(not self.stopped, "stopped")
            require(type(message) is dict, "input_invalid")
            if message == {"op": "history"}:
                require(not self.pending and self.rounds < 2, "history_order")
                value = await self._read("curation_history", {"request_id": REQUEST, "limit": 100})
                require(set(value) == {"items", "next_cursor"} and value["next_cursor"] is None
                        and isinstance(value["items"], list) and len(value["items"]) == 1, "history_incomplete")
                row = value["items"][0]
                require(row.get("request_id") == REQUEST and row.get("pax_run") == REQUEST
                        and row.get("source_id") == SOURCE, "history_identity_mismatch")
                # Original history is the sole field source; identity is comparison only.
                require(row.get("curator_client_id") == CLIENT, "curator_mismatch")
                decisions = row.get("decisions")
                require(isinstance(decisions, list) and 1 <= len(decisions) <= 4, "history_partial")
                required = {"action", "old_fact_ids", "new_fact_id", "visibility", "reason", "source_refs"}
                require(all(isinstance(d, dict) and required <= d.keys() and d["action"] == "add"
                            and type(d["new_fact_id"]) is int and d["new_fact_id"] > 0 for d in decisions), "history_partial")
                require(len({d["new_fact_id"] for d in decisions}) == len(decisions), "history_ambiguous")
                require(self.history is None or self.history == row, "history_changed")
                self.history = row
                self.rounds += 1
                self.pending = set(range(len(decisions)))
                return value
            require(set(message) == {"op", "index"} and message["op"] == "fact"
                    and type(message["index"]) is int and message["index"] in self.pending, "input_invalid")
            index = message["index"]
            fact_id = self.history["decisions"][index]["new_fact_id"]
            value = await self._read("list_facts", {"after_id": fact_id - 1, "limit": 1})
            facts = value.get("facts")
            require(isinstance(facts, list) and len(facts) == 1, "fact_incomplete")
            fact = facts[0]
            require(fact.get("id") == fact_id and fact.get("source_id") == SOURCE
                    and fact.get("source_session") == REQUEST, "fact_identity_mismatch")
            self.pending.remove(index)
            return value
        except BaseException:
            self.stopped = True
            raise


def open_reads():
    """Called only by the runtime facade in the admitted Pax host; never by a plugin."""
    from hermes_constants import get_hermes_home
    from tools import mcp_tool as core
    from tools.mcp_tool_scope import _server_key
    from tools.mcp_oauth_manager import get_manager
    require(get_hermes_home().resolve() == HOME, "profile_mismatch")
    # OWN connection only. No adoption, lazy connection, config read, auth build or recovery.
    key = _server_key("brain")
    def connected():
        with core._lock:
            return core._servers.get(key)
    server = connected()
    require(server is not None and server.session is not None, "not_connected")
    require(server._auth_type == "oauth" and server._config.get("url") == ENDPOINT, "route_mismatch")
    provider = get_manager().get_existing_provider("brain", ENDPOINT)
    require(provider is not None and getattr(provider, "_pax_wire_guard_installed", False), "transport_not_qualified")
    FrozenPaxAuth(provider, "curation_history", {"request_id": REQUEST, "limit": 100})
    return PaxRecoveryReads(server, provider, connected)


def serve_fd(fd):
    """Host-inherited socket only; no listener, path, process spawn, tool or CLI."""
    import socket
    from tools.mcp_tool_loop import _run_on_mcp_loop
    with socket.socket(fileno=fd) as channel:
        channel.settimeout(30)
        reader = open_reads()
        with channel.makefile("rwb", buffering=0) as stream:
            while True:
                line = stream.readline(4097)
                if not line:
                    return
                try:
                    require(len(line) <= 4096 and line.endswith(b"\n"), "frame_invalid")
                    message = json.loads(line)
                    value = _run_on_mcp_loop(lambda: reader.exchange(message), timeout=TIMEOUT + 1)
                    frame = {"ok": True, "value": value}
                except BaseException:
                    reader.stopped = True
                    channel.sendall(b'{"ok":false,"error":"pax_native_read_stopped"}\n')
                    raise
                data = json.dumps(frame, ensure_ascii=False).encode() + b"\n"
                require(len(data) <= MAX_BYTES + 1024, "frame_overflow")
                channel.sendall(data)
