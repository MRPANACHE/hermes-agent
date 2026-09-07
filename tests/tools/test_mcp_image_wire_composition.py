"""Real stdio MCP image -> registry -> history -> Responses, without a provider.

The model capability is a test setting; this does not prove model understanding
or the full agent turn loop. All Hermes state is isolated under scratch storage.
"""

import asyncio
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


def image_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


SOURCE = {
    "mailbox_id": "synthetic-mailbox",
    "message_id": "synthetic-message",
    "source_sha256": hashlib.sha256(b"synthetic MIME source").hexdigest(),
    "part_id": "1.2",
    "part_sha256": hashlib.sha256(image_bytes()).hexdigest(),
    "trust": "untrusted_source_content",
}


async def serve():
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name="read_image", description="Read synthetic source image.",
                     inputSchema={"type": "object", "properties": {}, "additionalProperties": False})])

    async def call_tool(context, params):
        if params.name != "read_image" or params.arguments:
            raise ValueError("unexpected fixture request")
        return CallToolResult(
            content=[TextContent(type="text", text="Synthetic attachment; no business authority."),
                     ImageContent(type="image", mimeType="image/png",
                                  data=base64.b64encode(image_bytes()).decode())],
            structuredContent=SOURCE,
        )

    server = Server("image-wire-fixture", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class TestMcpImageWireComposition(unittest.TestCase):
    def test_stdio_image_reaches_wire_and_text_fallback_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix="mcp-image-wire-", dir=os.environ.get("TMPDIR")) as home:
            with patch.dict(os.environ, {"HERMES_HOME": home}), ExitStack() as isolation:
                import tools.mcp_tool as mcp
                from tools.registry import ToolRegistry
                from run_agent import AIAgent
                from agent.tool_dispatch_helpers import make_tool_result_message
                from agent.codex_responses_adapter import (
                    _chat_messages_to_responses_input, _preflight_codex_input_items,
                )

                registry = ToolRegistry()
                isolation.enter_context(patch("tools.registry.registry", registry))
                for state in (mcp._server_trust_levels, mcp._tool_read_only_hints,
                              mcp._mcp_tool_server_names, mcp._server_error_counts,
                              mcp._server_breaker_opened_at):
                    isolation.enter_context(patch.dict(state))

                name = "image_wire_fixture"
                config = {
                    "command": sys.executable,
                    "args": [str(Path(__file__).resolve()), "--serve"],
                    "env": {"HERMES_HOME": home},
                    "image_content_mode": "multimodal",
                    "timeout": 10,
                }
                mcp._ensure_mcp_loop()
                server = None
                try:
                    async def start():
                        nonlocal server
                        server = mcp.MCPServerTask(name)
                        await server.start(config)
                        return server

                    server = mcp._run_on_mcp_loop(start, timeout=15)
                    mcp._servers[name] = server
                    registered = mcp._register_server_tools(name, server, config)
                    tool = next(item for item in registered if item.endswith("read_image"))
                    result = registry.dispatch(tool, {})
                    self.assertIsInstance(result, dict, "MCP must return multimodal content, not MEDIA text")
                    self.assertTrue(result.get("_multimodal"))

                    agent = object.__new__(AIAgent)
                    agent.provider = "synthetic-provider"
                    agent.model = "synthetic-model"
                    assistant = {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "call_source_image", "type": "function",
                        "function": {"name": tool, "arguments": "{}"},
                    }]}
                    for vision, lists in [(True, True), (False, True), (True, False)]:
                        with self.subTest(vision=vision, list_content=lists):
                            with patch.object(agent, "_model_supports_vision", return_value=vision), \
                                 patch.object(agent, "_provider_supports_vision_tool_messages", return_value=lists):
                                content = agent._tool_result_content_for_active_model(tool, result)
                            history = [{"role": "user", "content": "Read the synthetic attachment."}, assistant,
                                       make_tool_result_message(tool, content, "call_source_image")]
                            wire = _preflight_codex_input_items(_chat_messages_to_responses_input(history))
                            output = next(item["output"] for item in wire if item.get("type") == "function_call_output")
                            if vision and lists:
                                self.assertIsInstance(output, list)
                                images = [item for item in output if item["type"] == "input_image"]
                                self.assertEqual(len(images), 1)
                                url = images[0]["image_url"]
                                self.assertTrue(url.startswith("data:image/png;base64,"))
                                decoded = base64.b64decode(url.split(",", 1)[1], validate=True)
                                self.assertEqual(decoded, image_bytes())
                                self.assertEqual(hashlib.sha256(decoded).hexdigest(), SOURCE["part_sha256"])
                                text = "\n".join(item["text"] for item in output if item["type"] == "input_text")
                            else:
                                self.assertIsInstance(output, str)
                                self.assertIn("unavailable", output.lower())
                                self.assertNotIn("data:image/", output)
                                text = output
                            for value in SOURCE.values():
                                self.assertIn(value, text)
                            self.assertNotIn("MEDIA:", text)
                finally:
                    if server is not None:
                        mcp._run_on_mcp_loop(server.shutdown, timeout=15)
                    mcp._servers.pop(name, None)
                    mcp._stop_mcp_loop()


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        asyncio.run(serve())
    else:
        unittest.main()
