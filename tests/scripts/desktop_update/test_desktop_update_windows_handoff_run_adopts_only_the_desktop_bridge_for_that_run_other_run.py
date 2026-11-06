"""Native Windows case within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_handoff_run_adopts_only_the_desktop_bridge_for_that_run import (MARKER, Path, _creation_time, _finish, _script, pytest, subprocess, time)
from tests.scripts.desktop_update import test_desktop_update_windows_handoff_lineage as support

sleeper = support.sleeper

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('bridge', ['other-run'])
def test_handoff_run_adopts_only_the_desktop_bridge_for_that_run(
    tmp_path: Path, sleeper: subprocess.Popen, bridge: str,
) -> None:
    started = int(time.time()) - 20
    ct = _creation_time(sleeper.pid)
    if bridge == 'stale-ct':
        ct = f'{float(ct) - 5:.3f}'
    run = 'other.run' if bridge == 'other-run' else 'desk-1-ab-12cd'
    body = f'{sleeper.pid}\n{started}\n' + ('' if bridge == 'v1' else f'ct:{ct}\n') + f'run:{run}\n'
    (tmp_path / MARKER).write_bytes(body.encode())
    code, out = _finish(_script(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup',
                                '-DesktopPid', str(sleeper.pid), '-HandoffRun', 'desk-1-ab-12cd'))
    if bridge != 'ok':
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body.encode()
        return
    assert code == 0, out
    lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
    pid = out.split(' pid=')[1].split()[0]
    assert lines[0] == pid and lines[1] == str(started) and lines[2].startswith('ct:'), lines
    assert lines[3:] == ['run:desk-1-ab-12cd', ''], lines
