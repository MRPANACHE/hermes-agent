"""Internal capture for an explicitly bound MCP server; never execution authority.

The injected sink owns durable acceptance and retries of the detached observation.
This scope provides neither a pending outbox nor a tool-execution retry mechanism.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import re
from threading import Event
from typing import Callable
from uuid import uuid4


_CURRENT = ContextVar("mcp_observation_scope", default=None)
_MAX_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class McpObservationError(Exception):
    def __init__(self, invocation_id, *, execution_returned):
        self.invocation_id = invocation_id
        self.execution_returned = execution_returned
        super().__init__("mcp_observation_unconfirmed" if execution_returned else "mcp_observation_input_invalid")

    def result(self):
        return {"error": str(self), "invocation_id": self.invocation_id,
                "execution_returned": self.execution_returned, "observation_confirmed": False,
                "next_action": "retry_observation_only_do_not_repeat_tool" if self.execution_returned else "correct_observation_input"}


def _json(value):
    parents = set()

    def check(item, depth=0):
        if depth > 64:
            raise ValueError("invalid JSON depth")
        if type(item) is int and abs(item) > 2**53 - 1:
            raise ValueError("integer cannot be preserved by JavaScript JSON")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) not in (dict, list) or id(item) in parents:
            raise ValueError("invalid JSON value")
        parents.add(id(item))
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or key in ("__proto__", "prototype", "constructor"):
                    raise ValueError("invalid JSON key")
                check(child, depth + 1)
        else:
            for child in item:
                check(child, depth + 1)
        parents.remove(id(item))

    check(value)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("observation too large")
    return text


@dataclass(frozen=True)
class _Scope:
    binding_json: str
    server_name: str
    sink: Callable


@contextmanager
def mcp_observation_scope(*, agent_run_id, runtime_owner, native_session_id, execution_id,
                          intent_id, server_name, sink):
    """Install a trusted binding; callers must already own its OOS authorization.

    The async sink must raise unless the exact input has been accepted. Any retry
    is sink-only, using this invocation's detached input, never tools/call again.
    """
    binding = dict(agent_run_id=agent_run_id, runtime_owner=runtime_owner,
                   native_session_id=native_session_id, execution_id=execution_id, intent_id=intent_id)
    if any(type(value) is not str or not _ID.fullmatch(value) for value in [*binding.values(), server_name]) or not callable(sink):
        raise ValueError("mcp_observation_scope_invalid")
    scope = _Scope(_json(binding), server_name, sink)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


@dataclass(frozen=True)
class _Capture:
    scope: _Scope
    input_json: str
    invocation_id: str
    returned: Event = field(default_factory=Event)

    def arguments(self):
        return json.loads(self.input_json)["arguments"]

    async def record(self, result):
        self.returned.set()
        try:
            from mcp.types import CallToolResult

            if not isinstance(result, CallToolResult):
                raise ValueError("unknown MCP result")
            observation = json.loads(self.input_json)
            observation["result"] = result.model_dump(mode="json", by_alias=True)
            # Detach from both the MCP result and the immutable captured input.
            detached = json.loads(_json(observation))
            await self.scope.sink(detached)
        except Exception:
            raise McpObservationError(self.invocation_id, execution_returned=True) from None


def snapshot_mcp_observation(server_name, tool_name, arguments):
    scope = _CURRENT.get()
    if scope is None or scope.server_name != server_name:
        return None
    invocation_id = str(uuid4())
    try:
        if type(arguments) is not dict or type(tool_name) is not str or not _ID.fullmatch(tool_name):
            raise ValueError("invalid native input")
        payload = {**json.loads(scope.binding_json), "invocation_id": invocation_id,
                   "tool_name": tool_name, "arguments": arguments}
        return _Capture(scope, _json(payload), invocation_id)
    except Exception:
        raise McpObservationError(invocation_id, execution_returned=False) from None
