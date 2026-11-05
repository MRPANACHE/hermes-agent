"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker_release_ops import (MARKER, Path, _creation_time, _harness, pytest, subprocess, time)
from tests.scripts.desktop_update import test_desktop_update_windows_marker_release_ops as support

sleeper = support.sleeper

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
