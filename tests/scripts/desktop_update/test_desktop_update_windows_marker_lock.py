"""A7 on the real Windows hand-off script: every marker mutation happens inside one
hold of the kernel lock on ``<marker>.lock`` and the update child is the marker's
delegate before it runs a single instruction.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher
from tests.scripts.desktop_update.windows_handoff_support import (
    MARKER,
    POWERSHELL,
    HOLD_CLI,
    _creation_time,
    _alive,
    _dead_pid,
    _script,
    _finish,
    _op,
    _HeldLock,
    _custodian,
    _log,
)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()


# -- A7 ---------------------------------------------------------------------

@pytest.mark.platforms('windows')
def test_a7_claim_is_decided_inside_the_marker_lock(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    """While another process holds <marker>.lock the claimant must not touch a dead marker;
    once the holder (which replaced it with a live claim meanwhile) lets go, it judges THAT."""
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    marker = tmp_path / MARKER
    marker.write_bytes(dead)
    lock = _HeldLock(tmp_path)
    try:
        claimant = _script(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
        time.sleep(5)   # PowerShell start-up plus a few lock retries
        assert claimant.poll() is None, 'claimant did not wait for the marker lock'
        assert marker.read_bytes() == dead, 'a dead marker was mutated outside the marker lock'
        live = f'{sleeper.pid}\n{int(time.time())}\nct:{_creation_time(sleeper.pid)}\n'.encode()
        marker.write_bytes(live)   # the lock holder's own decision: a new live claim
    finally:
        lock.release()
    code, out = _finish(claimant)
    assert code == 2, out
    assert marker.read_bytes() == live


@pytest.mark.platforms('windows')
def test_a7_concurrent_claimants_over_a_dead_marker_yield_one_owner(tmp_path: Path) -> None:
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, HOLD_CLI)
    home = tmp_path / 'home'; home.mkdir()
    marker = home / MARKER
    marker.write_bytes(f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode())
    hold = tmp_path / 'release-update'
    lock = _HeldLock(home)   # line every claimant up behind the lock, then let them race
    claimants = [_script(home, install=install, HANDOFF_HOLD=str(hold)) for _ in range(4)]
    try:
        time.sleep(6)
        lock.release()
        deadline = time.monotonic() + 120
        while not Path(str(hold) + '.pid').exists():
            assert time.monotonic() < deadline, 'no claimant reached the update'
            time.sleep(0.05)
        while sum(c.poll() is not None for c in claimants) < 3:
            assert time.monotonic() < deadline, [c.poll() for c in claimants]
            time.sleep(0.1)
        refused = [c for c in claimants if c.poll() == 2]
        running = [c for c in claimants if c.poll() is None]
        assert len(running) == 1 and len(refused) == 3, [c.returncode for c in claimants]
        assert marker.read_bytes().decode().split('\n')[0] == _custodian(home, running[0].pid)
    finally:
        hold.touch()
        for c in claimants:
            if c.poll() is None:
                try:
                    c.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill', '/T', '/F', '/PID', str(c.pid)], capture_output=True, check=False)
    assert running[0].returncode == 0
    assert not marker.exists()


# -- the update child is the delegate before it runs anything ------------------

FIND_UPDATE_CHILD = r"""
param([string]$Control, [int]$Seconds)
# Cold PowerShell/CIM startup can exceed the production delegate lock's 10s
# window. Finish observer startup before the fixture opens that window.
$null = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
[IO.File]::WriteAllText((Join-Path $Control 'ready'), 'ready')
while (-not [IO.File]::Exists((Join-Path $Control 'go'))) { Start-Sleep -Milliseconds 20 }
$Parent = [int][IO.File]::ReadAllText((Join-Path $Control 'parent'))
$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    $row = Get-CimInstance Win32_Process -Filter "ParentProcessId=$Parent" |
        Where-Object { $_.CommandLine -match '--yes' } | Select-Object -First 1
    if ($row) { [Console]::Out.Write($row.ProcessId); exit 0 }
    Start-Sleep -Milliseconds 100
}
exit 1
"""

PROBE_GATED_CLI = HOLD_CLI.replace(
    "        print('--keep-stash'); return 0",
    """        Path(os.environ['HANDOFF_HELP_READY']).touch()
        while not Path(os.environ['HANDOFF_HELP_RELEASE']).exists():
            time.sleep(0.02)
        print('--keep-stash'); return 0""",
)


@pytest.mark.platforms('windows')
def test_script_killed_before_publishing_the_delegate_runs_no_update(tmp_path: Path) -> None:
    """Kill cell at the pre-publication boundary: windows.ps1 has created its `hermes update`
    child but has not published it as the marker delegate (the marker lock is held elsewhere).
    Killed there, no update instruction may have run, and the custodian line 1 names releases
    the marker (nothing holds the checkout)."""
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, PROBE_GATED_CLI)
    home = tmp_path / 'home'; home.mkdir()
    hold = tmp_path / 'release-update'
    ran = Path(str(hold) + '.pid')
    marker = home / MARKER
    finder = tmp_path / 'find_child.ps1'
    finder.write_text(FIND_UPDATE_CHILD, encoding='utf-8')
    control = tmp_path / 'observer'; control.mkdir()
    help_ready, help_release = control / 'help-ready', control / 'help-release'
    observer = subprocess.Popen([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(finder),
                                 '-Control', str(control), '-Seconds', '60'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    script = None
    lock = None
    try:
        deadline = time.monotonic() + 60
        while not (control / 'ready').exists():
            assert time.monotonic() < deadline and observer.poll() is None, 'CIM observer never became ready'
            time.sleep(0.02)
        script = _script(home, install=install, HANDOFF_HOLD=str(hold),
                         HANDOFF_HELP_READY=str(help_ready), HANDOFF_HELP_RELEASE=str(help_release))
        (control / 'parent').write_text(str(script.pid), encoding='utf-8')
        deadline = time.monotonic() + 60
        while not (marker.exists() and _custodian(home, script.pid)
                   and marker.read_bytes().split(b'\n')[0] == _custodian(home, script.pid).encode()):
            assert time.monotonic() < deadline and script.poll() is None, (
                'script never claimed', script.poll(),
                _log(home))
            time.sleep(0.02)
        while not help_ready.exists():
            assert time.monotonic() < deadline and script.poll() is None, ('help probe never became ready', _log(home))
            time.sleep(0.02)
        lock = _HeldLock(home)   # after the claim, before the delegate publication
        (control / 'go').touch()
        help_release.touch()
        output, error = observer.communicate(timeout=90)
        assert observer.returncode == 0, ('the update child never appeared', error, _log(home))
        child_pid = int(output)
        assert not ran.exists(), 'the update child ran before it was published as the delegate'
        subprocess.run(['taskkill', '/F', '/PID', str(script.pid)], capture_output=True, check=True)
        script.wait(timeout=30)
        time.sleep(3)
        assert not ran.exists(), 'the update child ran before it was published as the delegate'
        assert not _alive(child_pid), 'the never-resumed update child outlived its script'
        lines = marker.read_bytes().decode().split('\n')
        assert lines[0] == _custodian(home, script.pid), lines
        assert not any(line.startswith('delegate:') for line in lines), lines
    finally:
        help_release.touch()
        if observer.poll() is None:
            observer.kill(); observer.wait()
        if lock:
            lock.release()
        hold.touch()
        if script is not None and script.poll() is None:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(script.pid)], capture_output=True, check=False)
            script.wait()
    deadline = time.monotonic() + 60   # the custodian releases the marker once the lock is free
    while (verdict := _op(home, '-MarkerOp', 'reclaim')[1]).startswith('live '):
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert verdict in ('absent\n', 'reclaimed\n')
