"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker import (MARKER, Path, _BODIES, _creation_time, _run, pytest, subprocess, time)
from tests.scripts.desktop_update import test_desktop_update_windows_marker as support

sleeper = support.sleeper

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('case', ['garbled-ct-is-v1-fresh', 'garbled-ct-is-v1-past-20min', 'delegate-without-ct-is-ignored'])
def test_marker_bodies_are_parsed_positionally_like_every_other_reader(
    tmp_path: Path, sleeper: subprocess.Popen, case: str,
) -> None:
    template, verdict = _BODIES[case]
    now = int(time.time())
    body = template.format(pid=sleeper.pid, now=now, old=now - 1300, ct=_creation_time(sleeper.pid)).encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if verdict == 'live':
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
    else:
        assert code == 0, out
        assert (tmp_path / MARKER).read_bytes().decode().split('\n')[0] == str(pid)
