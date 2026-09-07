"""Trusted, context-local observation binding at the normal agent tool boundary."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import sys
from types import MappingProxyType
from uuid import uuid4

from tools.mcp_observation import _CURRENT, _ID, _Scope


@dataclass(frozen=True)
class _Execution:
    agent: object
    execution_id: str


_EXECUTION = ContextVar("native_observation_execution", default=None)


@contextmanager
def native_observation_execution(agent):
    """Isolate each admitted conversation, including an unconfigured child."""
    frame_token = _EXECUTION.set(_Execution(agent, str(uuid4())))
    capture_token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(capture_token)
        _EXECUTION.reset(frame_token)


@contextmanager
def native_observation_dispatch(agent, *, tool_name, task_id, tool_call_id):
    factory = getattr(agent, "_native_observation_scope_factory", None)
    if factory is None:
        yield
        return
    frame = _EXECUTION.get()
    if frame is None or frame.agent is not agent:
        raise RuntimeError("native_observation_execution_required")
    session_id = getattr(agent, "session_id", None)
    if type(session_id) is not str or _ID.fullmatch(session_id) is None:
        raise RuntimeError("native_observation_scope_invalid")
    metadata = MappingProxyType({
        "native_session_id": session_id,
        "execution_id": frame.execution_id,
        "tool_name": tool_name,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "turn_id": getattr(agent, "_current_turn_id", None),
        "api_request_id": getattr(agent, "_current_api_request_id", None),
    })
    token = _CURRENT.set(None)
    stack = ExitStack()
    try:
        try:
            scope = stack.enter_context(factory(metadata))
            if scope is None:
                if _CURRENT.get() is not None:
                    raise ValueError("unexpected scope")
            else:
                if not isinstance(scope, _Scope) or _CURRENT.get() is not scope:
                    raise ValueError("scope not installed")
                binding = json.loads(scope.binding_json)
                if (binding["native_session_id"] != session_id
                        or binding["execution_id"] != frame.execution_id):
                    raise ValueError("scope binding mismatch")
            if getattr(agent, "session_id", None) != session_id:
                raise ValueError("session changed during binding")
        except Exception:
            raise RuntimeError("native_observation_scope_invalid") from None
        yield
    finally:
        try:
            stack.__exit__(*sys.exc_info())
        except Exception:
            raise RuntimeError("native_observation_scope_invalid") from None
        finally:
            _CURRENT.reset(token)
