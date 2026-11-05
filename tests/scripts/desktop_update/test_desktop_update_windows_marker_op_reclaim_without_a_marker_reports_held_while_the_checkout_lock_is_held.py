"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker_checkout_ops import (MARKER, Path, _CHECKOUT_HOLDER, _op, pytest, subprocess, sys, time)

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
