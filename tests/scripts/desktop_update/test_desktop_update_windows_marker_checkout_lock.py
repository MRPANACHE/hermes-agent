"""Windows marker checkout lock contracts in a bounded native file worker."""
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
)

from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import (
    HARNESS,
    _CHECKOUT_HOLDER,
)

@pytest.mark.platforms('windows')
def test_marker_op_reclaim_reports_held_while_a_survivor_holds_the_checkout_lock(tmp_path: Path) -> None:
    """R6: the update died (dead marker) but a process it started still holds the checkout lock:
    the Desktop's reclaim must not free the marker; it answers `held` until that process exits."""
    install = tmp_path / 'hermes-agent'
    install.mkdir()
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    (tmp_path / MARKER).write_bytes(dead)
    release = tmp_path / 'release-holder'
    holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER, str(install / '.hermes-update.lock'), str(release)])
    try:
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'held\n')
        assert (tmp_path / MARKER).read_bytes() == dead
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'reclaimed\n')
    assert not (tmp_path / MARKER).exists()


@pytest.mark.platforms('windows')
def test_marker_op_reclaim_without_a_marker_reports_held_while_the_checkout_lock_is_held(tmp_path: Path) -> None:
    """Review 5411223284: `absent` was answered before the checkout lock was looked at, so a
    Desktop gate opened while an update with no marker (yet, or any more) held the checkout."""
    install = tmp_path / 'hermes-agent'
    install.mkdir()
    release = tmp_path / 'release-holder'
    holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER, str(install / '.hermes-update.lock'), str(release)])
    try:
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'held\n')
        assert not (tmp_path / MARKER).exists()
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'absent\n')


@pytest.mark.platforms('windows')
def test_marker_op_reclaim_reports_held_while_the_checkout_lock_cannot_be_opened(tmp_path: Path) -> None:
    """kshitij P2 (parity with marker.sh and the Desktop's probe): a lock file that exists but
    cannot be opened -- here another process holds it with no sharing -- read `not held`, so the
    reclaim deleted a dead marker the Desktop's own probe kept. It counts as held."""
    import ctypes

    install = tmp_path / 'hermes-agent'
    install.mkdir()
    lock = install / '.hermes-update.lock'
    lock.touch()
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    (tmp_path / MARKER).write_bytes(dead)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(str(lock), 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, no sharing
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    try:
        assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'held\n')
        assert (tmp_path / MARKER).read_bytes() == dead
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))
    assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'reclaimed\n')


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('lease', [1, 16])
def test_marker_op_reclaim_reports_held_while_a_leased_child_outlives_its_owner(tmp_path: Path, lease: int) -> None:
    """R5b: the owner died (its byte at 1 MiB is free) but a completion child the job refused
    still holds its lease byte. The checkout is busy until it exits: reclaim answers `held`."""
    install = tmp_path / 'hermes-agent'
    install.mkdir()
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    (tmp_path / MARKER).write_bytes(dead)
    release = tmp_path / 'release-holder'
    holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER, str(install / '.hermes-update.lock'),
                               str(release), str(lease)])
    try:
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        assert _op(tmp_path, '-MarkerOp', 'reclaim')[:2] == (0, 'held\n')
        assert (tmp_path / MARKER).read_bytes() == dead
    finally:
        release.touch()
        holder.wait(timeout=30)


def test_checkout_lock_probe_covers_the_owner_byte_and_every_lease_byte() -> None:
    """Runs everywhere (the live cells above need Windows): Test-CheckoutLockHeld must lock
    and unlock the same range hermes_cli/update_lock.py::_try_lock judges -- the owner byte at
    1 MiB plus the 16 R5b lease bytes after it -- or a leased survivor reads as a free checkout."""
    import re

    text = MARKER_PS1.read_text(encoding='utf-8-sig')
    body = text[text.index('function Test-CheckoutLockHeld'):]
    ranges = re.findall(r'\$fs\.(Lock|Unlock)\((\d+), (\d+)\)', body)
    assert ranges and {(int(o), int(n)) for _, o, n in ranges} == {(1 << 20, 1 + 16)}, ranges
    assert {kind for kind, _, _ in ranges} == {'Lock', 'Unlock'}


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

