"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker import (MARKER, Path, _live_protected_owner, _run, pytest, time)

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('ct_matches', [True, False])
def test_owner_creation_time_is_read_for_system_processes(tmp_path: Path, ct_matches: bool) -> None:
    """A1: a marker naming a SYSTEM/protected pid (a reused pid after a reboot) is judged by its
    real creation time, not treated as live forever because StartTime is access-denied."""
    owner, creation_time = _live_protected_owner()
    ct = creation_time if ct_matches else '1000.000'
    body = f'{owner}\n{int(time.time())}\nct:{ct}\n'.encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if ct_matches:
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
    else:
        assert code == 0, out
        assert (tmp_path / MARKER).read_bytes().decode().split('\n')[0] == str(pid)
