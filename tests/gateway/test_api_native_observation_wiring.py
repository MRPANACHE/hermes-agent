"""The normal API agent constructor receives only trusted Python injection."""

from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def construct_agent(adapter):
    with ExitStack() as stack:
        stack.enter_context(patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "api_key": "synthetic-key", "base_url": None, "provider": None,
            "api_mode": None, "command": None, "args": [],
        }))
        stack.enter_context(patch("gateway.run._resolve_gateway_model", return_value="synthetic/model"))
        stack.enter_context(patch("gateway.run._load_gateway_config", return_value={}))
        stack.enter_context(patch.object(adapter, "_ensure_session_db", return_value=None))
        constructor = stack.enter_context(patch("run_agent.AIAgent", return_value=MagicMock()))
        adapter._create_agent(session_id="caller-session")
        return constructor.call_args.kwargs


def test_explicit_python_factory_is_forwarded_to_normal_agent_constructor():
    factory = lambda context: nullcontext()
    adapter = APIServerAdapter(PlatformConfig(), native_observation_scope_factory=factory)
    assert construct_agent(adapter)["native_observation_scope_factory"] is factory


def test_configuration_extras_cannot_install_a_scope_factory():
    adapter = APIServerAdapter(PlatformConfig(extra={"native_observation_scope_factory": lambda context: nullcontext()}))
    assert "native_observation_scope_factory" not in construct_agent(adapter)


@pytest.mark.parametrize("factory", [False, "from-user-header", {}])
def test_noncallable_python_factory_is_rejected(factory):
    with pytest.raises(ValueError, match="native_observation_scope_factory_invalid"):
        APIServerAdapter(PlatformConfig(), native_observation_scope_factory=factory)
