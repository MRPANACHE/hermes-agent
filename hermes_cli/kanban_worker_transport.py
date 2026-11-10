"""Pre-admission transport selection for mixed-release Kanban profile fleets."""
from __future__ import annotations

import contextlib
import sys


def worker_argv(task, profile_arg, hermes_home):
    from hermes_cli.config import load_config
    from hermes_cli import kanban_db_dispatch as dispatch

    with (dispatch._worker_profile_scope(hermes_home) if hermes_home else contextlib.nullcontext()):
        transport = load_config().get("kanban", {}).get("worker_transport", "gateway")
    if transport == "gateway":
        return [sys.executable, "-m", "hermes_cli.kanban_worker_client"]
    if transport != "cli":
        raise ValueError("kanban.worker_transport must be 'gateway' or 'cli'")
    # This is a configured launch route, never an RPC-error fallback. An owner may have
    # committed admission before a transport failure; retrying it as CLI could run twice.
    cmd = [*dispatch._resolve_hermes_argv(), "-p", profile_arg, "--cli", "--accept-hooks"]
    for skill in task.skills or ():
        if skill:
            cmd.extend(["--skills", skill])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    toolsets = dispatch._resolve_worker_cli_toolsets(hermes_home)
    if toolsets:
        cmd.extend(["--toolsets", ",".join(toolsets)])
    return [*cmd, "chat", "-q", f"work kanban task {task.id}"]


def pin_worker_tree(env, command):
    # Module workers use the dispatcher's install. A CLI launcher owns its own release
    # selection and must not import the new dispatcher's tree into an older runtime.
    if command in ([sys.executable, "-m", "hermes_cli.kanban_worker_client"],
                   [sys.executable, "-P", "-m", "hermes_cli.kanban_command_worker"]):
        from pathlib import Path
        from hermes_cli._startup_fast import project_root_str
        from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
        return pin_hermes_tree_on_pythonpath(env, Path(project_root_str()))
    return env
