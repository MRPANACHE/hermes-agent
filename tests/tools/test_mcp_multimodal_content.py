"""Actual MCP handler and registration paths; synthetic transport results only."""
import asyncio
import base64
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tools import mcp_tool as mcp
from tools.registry import ToolRegistry

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


@pytest.fixture(autouse=True)
def isolate_registration_state():
    with ExitStack() as stack:
        for name in ("_server_trust_levels", "_tool_read_only_hints", "_lazy_server_configs",
                     "_lazy_server_fingerprints", "_lazy_server_tool_names", "_server_error_counts"):
            stack.enter_context(patch.dict(getattr(mcp, name), {}, clear=True))
        yield


def image(data=PNG, mime="image/png"):
    return SimpleNamespace(type="image", mimeType=mime, data=base64.b64encode(data).decode())


def result(blocks, error=False):
    return SimpleNamespace(content=blocks, isError=error, structuredContent={"source_sha256": "a" * 64}, meta={"example.org/evidence": "pinned"})


def server(value):
    return SimpleNamespace(session=SimpleNamespace(call_tool=AsyncMock(return_value=value)), _rpc_lock=asyncio.Lock(),
                           tool_timeout=17, _tools=[SimpleNamespace(name="image", description="Read an image", inputSchema={"type": "object"}, annotations={"readOnlyHint": True})],
                           _resources=[], _resource_templates=[], _prompts=[])


def call(handler, selected):
    def run(factory, timeout):
        assert timeout == 17
        return asyncio.run(factory())
    with patch.object(mcp, "_get_connected_server_for_call", return_value=selected), \
         patch.object(mcp, "_mark_server_call_started"), \
         patch.object(mcp, "_run_on_mcp_loop", side_effect=run), \
         patch.dict(mcp._server_error_counts, {}, clear=True):
        return handler({"source": "pinned"})


def test_multimodal_preserves_image_bytes_and_existing_metadata_without_cache():
    selected = server(result([SimpleNamespace(text="invoice context"), image()]))
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="multimodal")
    with patch.object(mcp, "_cache_mcp_image_block", side_effect=AssertionError("must not cache")):
        output = call(handler, selected)
    assert output["_multimodal"] is True
    metadata = json.loads(output["content"][0]["text"])
    assert metadata == {"result": "invoice context", "structuredContent": {"source_sha256": "a" * 64}, "_meta": {"example.org/evidence": "pinned"}}
    assert output["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}}
    assert "unavailable to text-only" in output["text_summary"]
    assert "invoice context" in output["text_summary"]
    assert "data:image" not in output["text_summary"]
    selected.session.call_tool.assert_awaited_once_with("image", arguments={"source": "pinned"})


@pytest.mark.parametrize("blocks", [
    [image(mime="image/gif")], [image(b"not png")],
    [SimpleNamespace(type="image", mimeType="image/png", data="%%%")],
    [SimpleNamespace(type="image", mimeType="image/png", data=base64.b64encode(PNG).decode() + "\n")],
    [SimpleNamespace(type="image", mimeType="image/png", data=None)],
    [image()] * 5,
    [image(PNG + b"x" * (4 * 1024 * 1024))],
    [image(PNG + b"x" * (3 * 1024 * 1024))] * 3,
])
def test_bad_or_excessive_images_refuse_whole_result(blocks):
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="multimodal")
    output = json.loads(call(handler, server(result([SimpleNamespace(text="not partial success"), *blocks]))))
    assert "mcp_image_content_invalid" in output["error"]
    assert "result" not in output
    assert "not partial success" not in json.dumps(output)


def test_jpeg_signature_transport_and_text_only_results():
    # Signature-only fixture: this tests transport, not JPEG decoding or vision.
    jpeg = b"\xff\xd8\xff\xe0synthetic\xff\xd9"
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="multimodal")
    output = call(handler, server(result([image(jpeg, "image/jpeg")])))
    assert output["content"][1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    text = call(handler, server(result([SimpleNamespace(text="text only")])))
    assert json.loads(text)["result"] == "text only"


def test_default_media_and_error_results_stay_text():
    handler = mcp._make_tool_handler("mm-test", "image", 17)
    with patch.object(mcp, "_cache_mcp_image_block", return_value="MEDIA:/synthetic/image.png") as cache:
        output = call(handler, server(result([image()])))
    assert json.loads(output)["result"] == "MEDIA:/synthetic/image.png"
    cache.assert_called_once()
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="multimodal")
    failed = call(handler, server(result([SimpleNamespace(text="provider refused"), image()], error=True)))
    assert "provider refused" in json.loads(failed)["error"]


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_registration_captures_config_for_real_handler(cached, enabled):
    selected = server(result([image()]))
    registry = ToolRegistry()
    config = {"tools": {"resources": False, "prompts": False}, "timeout": 17}
    if enabled:
        config["image_content_mode"] = "multimodal"
    entry = {"tools": [{"name": "image", "description": "Read image", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]}
    with patch("tools.registry.registry", registry), patch.object(mcp, "_track_mcp_tool_server"), \
         patch.object(mcp, "_scan_mcp_description"), patch.object(mcp, "_cache_mcp_image_block", return_value="MEDIA:/synthetic/image.png"):
        if cached:
            registered = mcp._register_from_cache_sync("mm-test", config, entry)
        else:
            registered = mcp._register_server_tools("mm-test", selected, config)
        assert len(registered) == 1
        config["image_content_mode"] = "media" if enabled else "multimodal"
        output = call(registry.get_entry(registered[0]).handler, selected)
    assert isinstance(output, dict) is enabled
    if enabled:
        assert output["_multimodal"] is True
    else:
        assert "MEDIA:" in output


def test_trust_refusal_precedes_transport_and_image_processing():
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="multimodal")
    with patch.object(mcp, "_trust_gate_check", return_value='{"error":"denied"}'), \
         patch.object(mcp, "_get_connected_server_for_call", side_effect=AssertionError("no transport")):
        assert handler({}) == '{"error":"denied"}'


def test_invalid_mode_fails_before_transport():
    handler = mcp._make_tool_handler("mm-test", "image", 17, image_content_mode="typo")
    with patch.object(mcp, "_get_connected_server_for_call", side_effect=AssertionError("no transport")):
        assert "mcp_image_content_mode_invalid" in json.loads(handler({}))["error"]
