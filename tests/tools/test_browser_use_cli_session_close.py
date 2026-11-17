"""A Browser Use harness daemon lives as long as the session that drove it.

The daemon outlives every browser_exec call on purpose (the session's tab and page state live in
it), so something must end it when the session ends. These tests drive the real ``browser_exec``
against a stand-in harness CLI that spawns a real detached stand-in daemon (own session, spawn tag,
published pid file) and stops it on ``--reload`` the way browser-harness does.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import process_identity
from tests.waiting import wait_until
from tools import browser_use_cli

# The fixture reaps stand-in daemons (real pids) the way the sibling owned-daemon tests do.
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.live_system_guard_bypass]

_FAKE_HARNESS = textwrap.dedent('''
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    pid_file = Path(os.environ["BH_RUNTIME_DIR"]) / f"bu-{os.environ.get('BU_NAME', 'default')}.pid"

    def live_pid():
        try:
            pid = int(json.loads(pid_file.read_text())["pid"])
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError, KeyError):
            return None

    if sys.argv[1:] == ["--reload"]:
        pid = live_pid()
        if pid:
            os.killpg(pid, signal.SIGTERM)
            pid_file.unlink(missing_ok=True)
        sys.exit(0)
    pid = live_pid()
    if pid is None:
        daemon = subprocess.Popen([sys.executable, "-m", "browser_harness.daemon"], cwd=os.environ["STANDIN_ROOT"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        pid_file.write_text(json.dumps({"pid": daemon.pid, "started": 1}))
        pid = daemon.pid
    sys.stdin.read()
    print(pid)
''')


def _alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)  # windows-footgun: ok — posix-marked test
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "pkgroot"
    (root / "browser_harness").mkdir(parents=True)
    (root / "browser_harness" / "__init__.py").write_text("", encoding="utf-8")
    (root / "browser_harness" / "daemon.py").write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    cli = tmp_path / "fake_harness.py"
    cli.write_text(_FAKE_HARNESS, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {**os.environ, "BH_RUNTIME_DIR": str(runtime), "BH_RUNTIME_DIR_SHARED": "1", "STANDIN_ROOT": str(root),
           **process_identity.spawn_env("browser-harness-daemon")}  # the daemon names this process as spawner
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: [sys.executable, str(cli)])
    monkeypatch.setattr(browser_use_cli, "_base_subprocess_env", lambda: dict(env))
    monkeypatch.setattr(browser_use_cli, "_route_exec_current", lambda e, *_a: (dict(e), None, False, 0))
    monkeypatch.setattr(browser_use_cli, "_driven_daemons", {})
    monkeypatch.setattr(browser_use_cli, "_daemon_drivers", {})
    monkeypatch.setattr(browser_use_cli, "_owned_daemon_groups", {})
    monkeypatch.setattr(browser_use_cli, "own_process_groups", lambda pgids: None)  # the supervisor is not under test
    monkeypatch.setattr(browser_use_cli, "register_child", lambda pid, purpose: None)
    spawned = []

    def exec_as(task_id, session=""):
        result = browser_use_cli.browser_exec("print('hi')", session=session, task_id=task_id)
        assert '"success": true' in result, result
        pid = int(json.loads(result)["output"])
        spawned.append(pid)
        return pid

    yield exec_as
    for pid in spawned:
        try:
            os.kill(pid, 9)  # windows-footgun: ok — posix-marked test
        except (ProcessLookupError, PermissionError):
            pass


def test_closing_the_session_stops_the_daemon_it_drove(harness):
    pid = harness("session-a")
    assert _alive(pid)

    browser_use_cli.stop_session_daemons({"session-a"})

    wait_until(lambda: not _alive(pid), timeout=15, what="the closed session's daemon to stop")
    assert browser_use_cli._driven_daemons == {} and browser_use_cli._daemon_drivers == {}


def test_a_daemon_another_open_session_drives_survives_until_its_last_driver_closes(harness):
    pid = harness("pippa-1", session="bolpippa")
    assert harness("pippa-2", session="bolpippa") == pid  # one daemon, two sessions

    browser_use_cli.stop_session_daemons({"pippa-1"})
    assert _alive(pid)
    assert harness("pippa-2", session="bolpippa") == pid  # still the same daemon, still driving

    browser_use_cli.stop_session_daemons({"pippa-2"})
    wait_until(lambda: not _alive(pid), timeout=15, what="the last driver's close to stop the daemon")


def test_per_turn_browser_cleanup_leaves_the_daemon_of_an_open_session(harness):
    """cleanup_browser runs at the end of every turn (non-headed mode); the session goes on."""
    from tools.browser_session_lifecycle import cleanup_browser
    pid = harness("session-a")

    cleanup_browser("session-a")

    assert _alive(pid)
    assert harness("session-a") == pid  # the next turn's call reaches the same daemon


def test_closing_a_session_never_touches_a_daemon_it_did_not_drive(harness):
    pid = harness("session-a")

    browser_use_cli.stop_session_daemons({"some-other-session", "", None})

    assert _alive(pid)


def test_the_next_browser_exec_after_a_stop_respawns_a_working_daemon(harness):
    old = harness("session-a")
    browser_use_cli.stop_session_daemons({"session-a"})
    wait_until(lambda: not _alive(old), timeout=15, what="the daemon to stop")

    new = harness("session-a")  # same task again: a fresh daemon answers

    assert new != old and _alive(new)
    browser_use_cli.stop_session_daemons({"session-a"})
    wait_until(lambda: not _alive(new), timeout=15, what="the respawned daemon to stop with its session")


def _agent_after_turns(session_id, *task_ids):
    """An agent whose turns ran under ``task_ids``, bound by the real turn-identity code (what
    run_conversation does at the start of every turn)."""
    from agent.turn_context import _bind_turn_identity
    agent = SimpleNamespace(session_id=session_id)
    for task_id in task_ids:
        _bind_turn_identity(agent, task_id, None, None, None, None)
    return agent


@pytest.mark.parametrize("session_id, task_ids", [
    ("sid-1", ("sid-1",)),                                    # gateway turn: task_id == session id
    ("sid-2", ("sid-2", "sid-2-after-compression")),          # a session id rotated by compression
    ("cron_job_2026", ("cron:job:5f3a",)),                    # cron teardown: its own task id
    ("child-session", ("subagent-0-ab12",)),                  # delegated child: task id != session id
])
def test_closing_an_agent_stops_the_daemons_of_the_tasks_its_turns_ran_under(harness, session_id, task_ids):
    """AIAgent.close() -> _close_task_resources stops by the agent's own turn task ids only."""
    from agent.client_lifecycle import ClientLifecycleMixin
    mine = [harness(task) for task in task_ids]
    other = harness("other-session", session="other")  # its own BU_NAME
    agent = _agent_after_turns(session_id, *task_ids)

    ClientLifecycleMixin._close_task_resources(agent, session_id)

    for pid in set(mine):
        wait_until(lambda pid=pid: not _alive(pid), timeout=15, what="the closed agent's daemon to stop")
    assert _alive(other)


def test_a_temporary_agent_on_the_live_session_id_never_stops_the_sessions_daemon(harness):
    """Gateway hygiene and /compress build a temporary agent with the live session's id (in-place
    compaction) that never runs a turn; closing it must leave the daemon the session still drives."""
    from agent.client_lifecycle import ClientLifecycleMixin
    pid = harness("live-sid")  # a gateway turn drives browser_exec with task_id == session id
    temporary = SimpleNamespace(session_id="live-sid")  # no _process_owner_task_ids: no turn ever ran

    ClientLifecycleMixin._close_task_resources(temporary, "live-sid")

    assert _alive(pid)
    ClientLifecycleMixin._close_task_resources(_agent_after_turns("live-sid", "live-sid"), "live-sid")
    wait_until(lambda: not _alive(pid), timeout=15, what="the live session's own close to stop its daemon")


def test_a_daemon_another_process_spawned_is_never_reloaded_by_a_session_close(harness, monkeypatch):
    """Harness dirs are per profile and the default BU_NAME is shared: a session can drive a daemon a
    CLI, a cron subprocess or another Hermes started and still uses. Closing drops our record only."""
    pid = harness("session-a")
    browser_use_cli._owned_daemon_groups.clear()  # not handed to our supervisor ...
    monkeypatch.setattr(browser_use_cli, "spawned_by_this_process", lambda _pid: False)  # ... nor tagged ours

    browser_use_cli.stop_session_daemons({"session-a"})

    assert _alive(pid)
    assert browser_use_cli._driven_daemons == {} and browser_use_cli._daemon_drivers == {}


def test_a_daemon_this_process_spawned_is_stopped_by_its_spawn_tag_alone(harness):
    pid = harness("session-a")
    browser_use_cli._owned_daemon_groups.clear()  # the tag in the daemon's environ still names this process

    browser_use_cli.stop_session_daemons({"session-a"})

    wait_until(lambda: not _alive(pid), timeout=15, what="the daemon this process spawned to stop")


def test_a_session_that_starts_driving_the_daemon_meanwhile_keeps_it(harness, monkeypatch):
    """The stop re-checks under the lock right before it reloads: a second session whose first
    browser_exec re-added itself in between must not lose the daemon it is using."""
    pid = harness("session-a", session="shared")
    real_lock = browser_use_cli._driven_daemons_lock

    class LateDriver:
        entered = 0

        def __enter__(self):
            type(self).entered += 1
            if type(self).entered == 2:  # the 1st entry picks candidates, the 2nd is the re-check: the late session lands first
                browser_use_cli._daemon_drivers.setdefault(next(iter(browser_use_cli._driven_daemons)), set()).add("late")
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(browser_use_cli, "_driven_daemons_lock", LateDriver())
    browser_use_cli.stop_session_daemons({"session-a"})

    assert _alive(pid)
    assert next(iter(browser_use_cli._daemon_drivers.values())) == {"late"}


def test_a_closed_viewer_session_stops_its_daemon_beside_the_soft_evict(harness):
    """The gateway owner ends a local session through _run_close_lifecycle: the cached agent is only
    soft-released there, so the daemon needs its own stop, with the ids the agent served."""
    from gateway.run_agent_cache import GatewayAgentCacheMixin
    from gateway.session_viewer_close import _run_close_lifecycle

    pid = harness("session-b")  # the compressed session's earlier task id
    other = harness("unrelated", session="other")  # its own BU_NAME, its own daemon
    agent = SimpleNamespace(session_id="session-b2", _process_owner_task_ids={"session-b", "session-b2"})
    runner = SimpleNamespace(_agent_cache={"route": (agent, "sig")}, _agent_cache_lock=None,
                             _evict_cached_agent_at_boundary=lambda key: None)
    runner._spawn_release_thread = lambda target, args, name, **_kw: target(*args)
    runner._stop_session_browser_daemons = lambda *a: GatewayAgentCacheMixin._stop_session_browser_daemons(runner, *a)
    authority = SimpleNamespace(runner=runner)

    asyncio.run(_run_close_lifecycle(authority, SimpleNamespace(route="route"), "session-b2", "cli", "cli_close"))

    wait_until(lambda: not _alive(pid), timeout=15, what="the closed session's daemon to stop")
    assert _alive(other)


def test_a_killed_managed_worker_takes_its_daemon_with_it(tmp_path):
    """A managed worker's daemon is owned like any process's: a SIGKILLed worker (no atexit) must not
    leave it behind for the owner to find. End to end through the real owner-death supervisor."""
    pkg = tmp_path / "pkgroot" / "browser_harness"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "daemon.py").write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    worker_src = (
        "import json, subprocess, sys, time\n"
        "from agent import runtime_session_store\n"
        "from tools import browser_use_cli\n"
        "runtime_session_store._worker_process = True\n"  # what a registered managed worker is
        f"d = subprocess.Popen([sys.executable, '-m', 'browser_harness.daemon'], cwd={str(tmp_path / 'pkgroot')!r},"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True,"
        " env=browser_use_cli._base_subprocess_env())\n"
        f"open({str(runtime / 'bu.pid')!r}, 'w', encoding='utf-8').write(json.dumps({{'pid': d.pid}}))\n"  # windows-footgun: ok — a write
        f"assert browser_use_cli._own_harness_daemon({{'BH_RUNTIME_DIR': {str(runtime)!r}}}) is True\n"
        "print(d.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    worker = subprocess.Popen([sys.executable, "-c", worker_src], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, text=True)
    daemon_pid = None
    try:
        daemon_pid = int(worker.stdout.readline())
        worker.kill()
        worker.wait(timeout=10)
        wait_until(lambda: not _alive(daemon_pid), timeout=15,
                   what="the daemon to die with the kill -9'd managed worker that drove it")
    finally:
        if daemon_pid is not None:
            try:
                os.killpg(daemon_pid, 9)  # windows-footgun: ok — posix-marked test
            except (ProcessLookupError, PermissionError):
                pass
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)
