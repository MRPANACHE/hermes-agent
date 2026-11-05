"""Native Windows contracts split at behavior boundaries; unchanged per-file timeout."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import time
import pytest
from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import _harness, HARNESS, _CHECKOUT_HOLDER
from tests.scripts.desktop_update import test_desktop_update_windows_marker_ops as marker_ops
from tests.scripts.desktop_update.windows_handoff_support import MARKER, MARKER_PS1, POWERSHELL, _creation_time, _dead_pid

sleeper = marker_ops.sleeper

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
