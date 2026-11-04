"""Standalone sends retain the profile's Telegram endpoint contract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("telegram")
import telegram.ext  # Import subclasses before replacing their Bot base.

from gateway.config import GatewayConfig, Platform
from tools.send_message_tool import _send_to_platform


@pytest.mark.parametrize("extra,expected", [
    ({}, {}),
    ({"base_url": "http://localhost:9482/bot"},
     {"base_url": "http://localhost:9482/bot", "base_file_url": "http://localhost:9482/bot"}),
    ({"base_url": "http://localhost:9482/bot", "base_file_url": "http://localhost:9482/file/bot"},
     {"base_url": "http://localhost:9482/bot", "base_file_url": "http://localhost:9482/file/bot"}),
])
@pytest.mark.parametrize("proxy", [None, "http://localhost:8080", "broken"])
@pytest.mark.parametrize("route", ["tool", "plugin"])
def test_profile_endpoints_reach_standalone_bot(extra, expected, proxy, route):
    config = GatewayConfig.from_dict({"platforms": {"telegram": {
        "enabled": True, "token": "123:test", "extra": extra,
    }}})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    with patch("telegram.Bot", return_value=bot) as constructor, patch(
        "gateway.platforms.base.resolve_proxy_url", return_value=proxy
    ), patch("telegram.request.HTTPXRequest") as request:
        if proxy == "broken":
            request.side_effect = RuntimeError("proxy unavailable")
        pconfig = config.platforms[Platform.TELEGRAM]
        if route == "tool":
            result = asyncio.run(_send_to_platform(Platform.TELEGRAM, pconfig, "12345", "hello"))
        else:
            from plugins.platforms.telegram.adapter import _standalone_send
            result = asyncio.run(_standalone_send(pconfig, "12345", "hello"))
    assert "error" not in result, result
    kwargs = dict(constructor.call_args.kwargs)
    assert kwargs.pop("token") == "123:test"
    if proxy and proxy != "broken":
        kwargs.pop("request")
        kwargs.pop("get_updates_request")
    assert kwargs == expected
    bot.send_message.assert_awaited_once()
