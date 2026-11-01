"""Direct cron workers must retain profile endpoints, just like gateways."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("telegram")
import telegram.ext  # Load subclasses before replacing their Bot base.

from cron import scheduler, scheduler_delivery


@pytest.mark.parametrize("for_failure", [False, True])
def test_direct_cron_delivery_uses_configured_telegram_endpoint(
    tmp_path, monkeypatch, for_failure,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps({
        "platforms": {"telegram": {
            "enabled": True,
            "token": "123:test",
            "extra": {
                "base_url": "http://localhost:9482/bot",
                "base_file_url": "http://localhost:9482/file/bot",
            },
        }},
    }))
    monkeypatch.setattr(scheduler, "load_config", lambda: {
        "cron": {"mirror_delivery": False},
    })
    monkeypatch.setattr(scheduler_delivery, "_record_delivery_verification", lambda *_: None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    job = {"id": "endpoint-contract", "name": "Endpoint contract",
           "deliver": "telegram:12345", "failure_deliver": "telegram:12345"}
    with patch("telegram.Bot", return_value=bot) as constructor, patch(
        "gateway.platforms.base.resolve_proxy_url", return_value=None,
    ):
        result = scheduler_delivery._deliver_result(
            job, "Existing saved output", adapters={}, for_failure=for_failure,
        )
    assert result is None, result
    assert constructor.call_args.kwargs["base_url"] == "http://localhost:9482/bot"
    assert constructor.call_args.kwargs["base_file_url"] == "http://localhost:9482/file/bot"
    bot.send_message.assert_awaited_once()
