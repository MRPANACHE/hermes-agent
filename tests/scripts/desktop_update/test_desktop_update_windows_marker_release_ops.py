"""Native Windows contracts split at behavior boundaries; unchanged per-file timeout."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import time
import pytest
from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import _harness, HARNESS, _CHECKOUT_HOLDER
from tests.scripts.desktop_update import test_desktop_update_windows_marker_ops as marker_ops
from tests.scripts.desktop_update.windows_handoff_support import MARKER, MARKER_PS1, POWERSHELL, _creation_time, _dead_pid

sleeper = marker_ops.sleeper

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('delegate', ['live', 'dead'])
def test_release_hands_the_claim_to_a_live_delegate(
    tmp_path: Path, sleeper: subprocess.Popen, delegate: str,
) -> None:
    """A7 rule 5 (the crash-12db shape from the owner's side): the releasing owner deletes the
    marker unless its delegate still runs -- then that delegate becomes the owner."""
    dpid = sleeper.pid if delegate == 'live' else _dead_pid()
    dct = _creation_time(sleeper.pid) if delegate == 'live' else '5.000'
    started = int(time.time()) - 60
    _harness(tmp_path, f'{{self}}\n{started}\nct:{{selfct}}\ndelegate:{dpid} ct:{dct}\nrun:desk-3\n', 'release')
    if delegate == 'dead':
        assert not (tmp_path / MARKER).exists()
    else:
        assert (tmp_path / MARKER).read_bytes().decode() == f'{dpid}\n{started}\nct:{dct}\nrun:desk-3\n'


@pytest.mark.platforms('windows')
def test_release_rereads_a_cached_delegate_identity_before_handing_over(
    tmp_path: Path, sleeper: subprocess.Popen,
) -> None:
    """Get-LiveProcessCt forgets a pid only when it SEES it dead: a delegate that exited and
    whose pid was reused between two polls keeps its cached creation time. The release must
    re-read it, or the marker is handed to the unrelated process now wearing that pid."""
    started = int(time.time()) - 60
    stale = f'{{self}}\n{started}\nct:{{selfct}}\ndelegate:{sleeper.pid} ct:5.000\nrun:desk-3\n'
    _harness(tmp_path, stale, 'release', '-StaleCt', f'{sleeper.pid}:5.0')
    assert not (tmp_path / MARKER).exists(), (tmp_path / MARKER).read_bytes()


@pytest.mark.platforms('windows')
def test_heartbeat_refreshes_line_2_only_for_our_own_claim(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    started = int(time.time()) - 900
    dct = _creation_time(sleeper.pid)
    pid, ct = _harness(tmp_path, f'{{self}}\n{started}\nct:{{selfct}}\ndelegate:{sleeper.pid} ct:{dct}\nrun:r\n',
                       'heartbeat')
    lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
    assert lines[0] == pid and lines[2:] == [f'ct:{ct}', f'delegate:{sleeper.pid} ct:{dct}', 'run:r', '']
    assert int(time.time()) - int(lines[1]) < 60, lines
    foreign = f'{sleeper.pid}\n{started}\nct:{dct}\n'
    _harness(tmp_path, foreign, 'heartbeat')
    assert (tmp_path / MARKER).read_bytes().decode() == foreign


@pytest.mark.platforms('windows')
def test_release_wait_keeps_line_2_young_while_the_checkout_lock_is_held(tmp_path: Path) -> None:
    """R6 wait + old packaged Desktops: while the release waits on a survivor's checkout lock
    (up to hours), the heartbeat keeps line 2 young, or an old Desktop age-deletes the marker."""
    install = tmp_path / 'hermes-agent'
    install.mkdir()
    marker = tmp_path / MARKER
    release = tmp_path / 'release-holder'
    holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER, str(install / '.hermes-update.lock'), str(release)])
    harness = tmp_path / 'harness.ps1'
    harness.write_text(HARNESS, encoding='utf-8')
    started = int(time.time()) - 900
    proc = None
    try:
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        proc = subprocess.Popen([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                                 '-MarkerPs1', str(MARKER_PS1), '-Marker', str(marker), '-Body', f'{{self}}\n{started}\nct:{{selfct}}\n',
                                 '-Action', 'release', '-InstallRoot', str(install), '-Heartbeat', '1'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        seen = []
        for _ in range(2):
            time.sleep(4)
            assert proc.poll() is None, proc.communicate()
            lines = marker.read_bytes().decode().split('\n')
            assert time.time() - int(lines[1]) <= 3, lines
            seen.append(int(lines[1]))
        assert seen[1] > seen[0]
    finally:
        release.touch()
        holder.wait(timeout=30)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, out + err
    assert not marker.exists()
