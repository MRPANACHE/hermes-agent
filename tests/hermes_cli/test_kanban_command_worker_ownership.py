"""A killed command supervisor must not leave its disposable command running."""
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from tests.waiting import wait_until


def _alive(process):
    try:
        process.wait(timeout=0)
        return False
    except psutil.TimeoutExpired:
        return True
    except psutil.NoSuchProcess:
        return False


@pytest.mark.platforms("posix")
def test_command_dies_when_its_supervisor_is_killed(tmp_path):
    marker = tmp_path / "command.pid"
    command = [sys.executable, "-c",
               "import os,sys,threading;from pathlib import Path;"
               "Path(sys.argv[1]).write_text(str(os.getpid()));threading.Event().wait(60)",
               str(marker)]
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_KANBAN_TASK_ID="disposable-containment",
               HERMES_KANBAN_WORKER_COMMAND=json.dumps(command),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    command_process = None
    supervisor = subprocess.Popen([sys.executable, "-m", "hermes_cli.kanban_command_worker"],
                                  env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: marker.exists() and marker.read_text().strip(), timeout=20)
        command_process = psutil.Process(int(marker.read_text()))
        assert _alive(command_process)
        supervisor.kill()
        supervisor.wait(timeout=20)
        wait_until(lambda: not _alive(command_process), timeout=20,
                   what="command teardown after supervisor death")
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=20)
        if command_process is not None and _alive(command_process):
            command_process.kill()
            command_process.wait(timeout=20)
