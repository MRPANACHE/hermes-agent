"""Windows marker release contracts in a bounded native file worker."""
from __future__ import annotations
from pathlib import Path
import subprocess
import time
import pytest
from tests.scripts.desktop_update.windows_handoff_support import (
    MARKER,
    _creation_time,
    _dead_pid,
)

from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import (
    _harness,
    sleeper,
)

__all__ = ['sleeper']

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('delegate', ['live', 'dead'])
def test_release_hands_the_claim_to_a_live_delegate(
    tmp_path: Path, sleeper: subprocess.Popen, delegate: str,
) -> None:
    """A7 rule 5 (the crash-12db shape from the owner's side): the releasing owner deletes the
    marker unless its delegate still runs -- then that delegate becomes the owner."""
    dpid = sleeper.pid if delegate == 'live' else _dead_pid()
    dct = _creation_time(sleeper.pid) if delegate == 'live' else '5.000'
    started = int(time.time()) - 60
    _harness(tmp_path, f'{{self}}\n{started}\nct:{{selfct}}\ndelegate:{dpid} ct:{dct}\nrun:desk-3\n', 'release')
    if delegate == 'dead':
        assert not (tmp_path / MARKER).exists()
    else:
        assert (tmp_path / MARKER).read_bytes().decode() == f'{dpid}\n{started}\nct:{dct}\nrun:desk-3\n'


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

