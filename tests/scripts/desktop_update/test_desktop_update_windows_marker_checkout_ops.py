"""Native Windows contracts split at behavior boundaries; unchanged per-file timeout."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import time
import pytest
from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import _CHECKOUT_HOLDER
from tests.scripts.desktop_update.windows_handoff_support import MARKER, _dead_pid, _op

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
