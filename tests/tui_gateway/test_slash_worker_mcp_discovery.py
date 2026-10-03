"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import yaml

_mcp_server_mod = pytest.importorskip("mcp.server")

if not hasattr(_mcp_server_mod, "MCPServer"):
    # `mcp.server.MCPServer` replaced `mcp.server.fastmcp.FastMCP` in mcp 2.0.
    # Skip rather than fail on a FastMCP-era SDK: the probe below is written
    # against the 2.x API, and the pinned version provides it.
    pytest.skip(
        "profile-local MCP discovery probe requires mcp >= 2.0 (MCPServer)",
        allow_module_level=True,
    )


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "mcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                # CI runs this integration probe inside a heavily parallel
                # slice. Give the local stdio MCP server enough bounded
                # startup time so the first /tools response proves profile
                # discovery instead of racing the default 1.5s interactive
                # startup budget.
                "mcp_discovery_timeout": 60.0,
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdout = proc.stdout
        def _read_stdout() -> None:
            for line in stdout:
                output.put(line)

        threading.Thread(target=_read_stdout, daemon=True).start()

        expected_tool = "mcp__profileprobe__hermes_61922_profile_probe"
        deadline = time.monotonic() + 120.0
        last_output = ""
        request_id = 0
        while time.monotonic() < deadline:
            request_id += 1
            proc.stdin.write(json.dumps({"id": request_id, "command": "/tools"}) + "\n")
            proc.stdin.flush()
            remaining = max(0.1, min(30.0, deadline - time.monotonic()))
            try:
                line = output.get(timeout=remaining)
            except queue.Empty:
                if proc.poll() is not None:
                    pytest.fail(f"slash worker exited before /tools response: {proc.returncode}")
                continue
            response = json.loads(line)
            assert response["ok"] is True
            last_output = response["output"]
            if expected_tool in last_output:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"profile-local MCP tool was not visible in /tools output: {last_output}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
