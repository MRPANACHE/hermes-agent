"""Protocol-2 helper ops (``windows.ps1 -MarkerOp reclaim|withdraw``) and the
production release / heartbeat functions, on real processes and files.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.scripts.desktop_update.windows_handoff_support import (
    MARKER_PS1,
    MARKER,
    POWERSHELL,
    _dead_pid,
    _op,
    _HeldLock,
)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()


# -- helper ops ---------------------------------------------------------------



@pytest.mark.platforms('windows')
def test_marker_op_reports_busy_while_the_lock_is_held(tmp_path: Path) -> None:
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    (tmp_path / MARKER).write_bytes(dead)
    lock = _HeldLock(tmp_path)
    try:
        assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'busy\n')
    finally:
        lock.release()
    assert (tmp_path / MARKER).read_bytes() == dead




# -- release / heartbeat (production functions in a real PowerShell process) ---

HARNESS = r"""
param([string]$MarkerPs1, [string]$Marker, [string]$Body, [string]$Action, [string]$InstallRoot = '', [int]$Heartbeat = 300,
      [string]$StaleCt = '')
$MarkerPath = $Marker
$NoMarkerCleanup = $false
function Write-HandoffLog([string]$Message) { [Console]::Error.WriteLine($Message) }
. $MarkerPs1
$script:MarkerHeartbeatSeconds = $Heartbeat
if ($StaleCt) {   # "<pid>:<ct>": what the cache kept for an earlier incarnation of that pid
    $stale = $StaleCt.Split(':')
    $script:ProcessCtCache[[int]$stale[0]] = [double]::Parse($stale[1], [Globalization.CultureInfo]::InvariantCulture)
}
$own = Format-Ct (Get-LiveProcessCt $PID).Ct
[System.IO.File]::WriteAllText($MarkerPath, $Body.Replace('{self}', "$PID").Replace('{selfct}', $own))
$script:MarkerClaim = 'claimed'
if ($Action -eq 'release') { Invoke-MarkerRelease }
if ($Action -eq 'heartbeat') { $script:MarkerHeartbeatSeconds = 0; Update-MarkerHeartbeat }
[Console]::Out.Write("$PID $own")
"""


def _harness(tmp_path: Path, body: str, action: str, *extra: str) -> tuple[str, str]:
    harness = tmp_path / 'harness.ps1'
    harness.write_text(HARNESS, encoding='utf-8')
    proc = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                           '-MarkerPs1', str(MARKER_PS1), '-Marker', str(tmp_path / MARKER),
                           '-Body', body, '-Action', action, *extra],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pid, ct = proc.stdout.split()
    return pid, ct








_CHECKOUT_HOLDER = """
import msvcrt, os, sys, time
from pathlib import Path
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
# offset 1 MiB: exactly hermes_cli/update_lock.py::_try_lock; 1 MiB + 1..16: one R5b lease byte
os.lseek(fd, (1 << 20) + (int(sys.argv[3]) if len(sys.argv) > 3 else 0), os.SEEK_SET)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
Path(sys.argv[2] + '.ready').write_text('1', encoding='utf-8')
while not Path(sys.argv[2]).exists():
    time.sleep(0.05)
"""












