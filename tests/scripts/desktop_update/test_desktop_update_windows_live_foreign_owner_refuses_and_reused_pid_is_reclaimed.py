"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker import (MARKER, Path, _creation_time, _run, pytest, subprocess, time)
from tests.scripts.desktop_update import test_desktop_update_windows_marker as support

sleeper = support.sleeper

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('ct_matches', [True, False])
def test_live_foreign_owner_refuses_and_reused_pid_is_reclaimed(
    tmp_path: Path, sleeper: subprocess.Popen, ct_matches: bool,
) -> None:
    ct = _creation_time(sleeper.pid) if ct_matches else '1000.000'   # wrong ct = pid reuse
    body = f'{sleeper.pid}\n{int(time.time())}\nct:{ct}\n'.encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if ct_matches:
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
        # A refused run changed nothing and owns no result (A4): the other update reports.
        assert not (tmp_path / '.hermes-update-result.json').exists()
    else:
        assert code == 0, out
        lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
        assert lines[0] == str(pid) and lines[2].startswith('ct:'), lines
