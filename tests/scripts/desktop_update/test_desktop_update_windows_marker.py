"""Update-marker contract C1 v2 on the real Windows hand-off script.

Every case runs the real ``scripts/desktop-update/windows.ps1`` under
Windows PowerShell against a marker owned by a REAL process (a sleeping
Python child) and reads the marker / result / hand-off log it leaves.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher
from tests.scripts.desktop_update.legacy_desktop_reader import legacy_read
from tests.scripts.desktop_update.windows_handoff_support import _HeldLock

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = ROOT / 'scripts/desktop-update/windows.ps1'
MARKER = '.hermes-update-in-progress'
CLI = """
import sys
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()


def _creation_time(pid: int) -> str:
    # Independent kernel observation, without spawning a second WMI consumer.
    milliseconds = int(psutil.Process(pid).create_time() * 1000)
    return f'{milliseconds / 1000:.3f}'


def _protected_pid(exclude: set[int] | frozenset[int] = frozenset()) -> int:
    """A live SYSTEM/protected pid whose creation time Windows PowerShell's Get-Process .StartTime
    cannot read from this token while Win32_Process.CreationDate (CIM) can: the A1 case.

    Which pids those are depends on the token. A non-elevated developer shell gets no rights at
    all on csrss/smss/services/lsass, so StartTime reads $null there; .NET Framework reads
    StartTime with limited query rights, so a pid granting those (what an elevated runner gets on
    the same services) is NOT the A1 case. Pick by the observed StartTime/CIM split first; only a
    token that sees no such pid falls back to a pid granting only limited query rights. Prefer the
    named services, then the oldest pid, so the owner outlives the test."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    preferred = ('csrss.exe', 'smss.exe', 'wininit.exe', 'services.exe', 'lsass.exe', 'MsMpEng.exe')
    rows = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-CimInstance Win32_Process | ForEach-Object { '
         '$p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; '
         '$st = $false; if ($p) { try { $st = $null -ne $p.StartTime } catch {} }; '
         # A process that exits during the scan also reads as a hidden StartTime. On a busy elevated runner
         # those are the only such rows, and they outrank the real candidates, so drop a row whose pid is gone.
         'if (-not $st -and -not (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue)) { return }; '
         '$ct = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().Ticks } else { 0 }; '
         '\'{0} {1} {2} {3}\' -f $_.ProcessId, $ct, [int]$st, $_.Name }'],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout.splitlines()
    candidates = []
    for row in rows:
        pid, ticks, start_time_readable, name = row.strip().split(' ', 3)
        if int(pid) in (0, 4, os.getpid()) or not int(ticks) or int(pid) in exclude:
            continue
        if start_time_readable == '0':
            candidates.append((0, name not in preferred, int(ticks), int(pid)))
            continue
        full = k32.OpenProcess(0x0400, False, int(pid))         # PROCESS_QUERY_INFORMATION
        limited = k32.OpenProcess(0x1000, False, int(pid))      # ..._LIMITED_INFORMATION
        for handle in (full, limited):
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
        if limited and not full:
            candidates.append((1, name not in preferred, int(ticks), int(pid)))
    if candidates:
        return min(candidates)[3]
    pytest.fail('no SYSTEM/protected process here hides its StartTime or grants only limited '
                'query rights: ' + ', '.join(rows[:40]))


def _live_protected_owner() -> tuple[int, str]:
    """A protected pid that is STILL alive and CIM-readable at the moment the
    marker scenario starts, with its creation time.

    On a busy CI runner the scan can pick a short-lived SYSTEM process that
    exits (or a WMI hiccup refuses the re-read) before the test uses it —
    the pick-and-read pair is retried, excluding each vanished pid, so the
    owner is one that actually outlives the test."""
    excluded: set[int] = set()
    for _ in range(5):
        owner = _protected_pid(exclude=excluded)
        try:
            return owner, _creation_time(owner)
        except (psutil.NoSuchProcess, subprocess.CalledProcessError, ValueError):
            excluded.add(owner)
    pytest.fail('no live SYSTEM/protected process stayed CIM-readable across attempts: '
                + ', '.join(str(pid) for pid in sorted(excluded)))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def _run(home: Path, *args: str, install: Path | None = None, timeout: int = 120):
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
               '-InstallRoot', str(install or home / 'hermes-agent'), '-NoUi', *args]
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'empty-store')}
    proc = subprocess.Popen(command, cwd=home, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, check=False)
        out, _ = proc.communicate()
        pytest.fail(f'hand-off did not finish within {timeout}s: {out}')
    return proc.pid, proc.returncode, out


def _result(home: Path) -> dict:
    return json.loads((home / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))


@pytest.mark.platforms('windows')
def test_claim_is_the_first_action_and_writes_a_v2_body(tmp_path: Path) -> None:
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    assert code == 0, out
    lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
    assert lines[0] == str(pid)
    assert lines[2].startswith('ct:') and len(lines[2].split('.')[-1]) == 3, lines
    log = (tmp_path / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig').splitlines()
    # Nothing (UI, Add-Type'd console helpers, probes) logs before the claim.
    assert 'claimed update marker' in log[0], log






_BODIES = {   # contract A2: identical verdicts in every reader
    'crlf-bom-v2': ('\ufeff{pid}\r\n{now}\r\nct:{ct}\r\n', 'live'),
    'missing-line-2': ('{pid}\n', 'dead'),
    'garbled-line-2': ('{pid}\nsoon\nct:{ct}\n', 'dead'),
    'fractional-started-at': ('{pid}\n{now}.5\nct:{ct}\n', 'dead'),
    'garbled-ct-is-v1-fresh': ('{pid}\n{now}\nct:garbage\n', 'live'),
    'garbled-ct-is-v1-past-20min': ('{pid}\n{old}\nct:garbage\n', 'dead'),
    'delegate-without-ct-is-ignored': ('999999\n{now}\nct:1.000\ndelegate:{pid}\n', 'dead'),
}














HOLD_CLI = """
import os, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    if sys.argv[1:2] == ['update']:   # still starting up: report the pid, wait to be released
        hold = Path(os.environ['HANDOFF_HOLD'])
        Path(str(hold) + '.pid').write_text(str(os.getpid()), encoding='utf-8')
        while not hold.exists():
            time.sleep(0.05)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


def _marker_lines(path: Path) -> list[str]:
    try:   # the script may be mid-replace (sharing violation): read again next poll
        return path.read_bytes().decode().splitlines()
    except OSError:
        return []


def _parent_pid(pid: int) -> int:
    out = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    return int(out or 0)
