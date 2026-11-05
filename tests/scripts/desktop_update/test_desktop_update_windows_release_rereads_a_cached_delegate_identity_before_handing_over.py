"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker_release_ops import (MARKER, Path, _harness, pytest, subprocess, time)
from tests.scripts.desktop_update import test_desktop_update_windows_marker_release_ops as support

sleeper = support.sleeper

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
