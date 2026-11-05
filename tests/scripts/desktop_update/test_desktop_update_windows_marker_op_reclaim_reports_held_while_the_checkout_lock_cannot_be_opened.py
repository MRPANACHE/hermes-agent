"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker_checkout_ops import (MARKER, Path, _dead_pid, _op, pytest, time)

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
