"""Windows handoff lineage environment contracts in a bounded native file worker."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import pytest
from tests.scripts.desktop_update.lineage_rule_cases import ENV_CASES, RULE_CASES
from tests.scripts.desktop_update.windows_handoff_support import (
    MARKER,
    MARKER_PS1,
    POWERSHELL,
    _wait_for_log,
)

from tests.scripts.desktop_update.test_desktop_update_windows_handoff_lineage import (
    _PASCAL,
    _RULE_HARNESS,
    _old_desktop,
    sleeper,
)

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('env_matches', [True, False], ids=['env-matches', 'env-differs'])
def test_r4_live_wrapper_not_the_desktops_child_is_adopted_only_with_the_handoff_started_at(
    tmp_path: Path, sleeper: subprocess.Popen, env_matches: bool,
) -> None:
    """The lineage table's divergent row (round 5 D11) on real processes: the cmd.exe wrapper is
    our live parent but not the Desktop's child; line 2 == HERMES_UPDATE_STARTED_AT adopts, as
    in bash. Any other startedAt refuses."""
    home = tmp_path / 'home'; home.mkdir()
    desktop, wrapper, started, _ = _old_desktop(home, 'waiting-wrapper', other_desktop=sleeper.pid,
                                                env_skew=0 if env_matches else -7)
    try:
        log = _wait_for_log(home, 'hand-off start:' if env_matches else 'exiting without claiming')
    finally:
        desktop.kill(); desktop.wait()
    if env_matches:
        assert 'marker=adopted' in log, log
        assert f"desktop pid {sleeper.pid}'s launcher pid {wrapper}" in log, log
        lines = (home / MARKER).read_bytes().decode().split('\n')
        assert lines[1] == str(started) and lines[2].startswith('ct:'), lines
    else:
        assert 'hand-off start:' not in log, log
        assert (home / MARKER).read_bytes().decode() == f'{wrapper}\n{started}\n'


@pytest.mark.platforms('windows')
def test_launcher_lineage_rule_matches_the_shared_table(tmp_path: Path) -> None:
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps({
        'rule': [{'id': c['id'], 'facts': {_PASCAL[k]: v for k, v in c['facts'].items()}} for c in RULE_CASES],
        'env': [{'id': c['id'], 'env': c['env'], 'line2': c['line2']} for c in ENV_CASES],
    }), encoding='utf-8')
    harness = tmp_path / 'rule.ps1'
    harness.write_text(_RULE_HARNESS, encoding='utf-8')
    proc = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                           '-MarkerPs1', str(MARKER_PS1), '-Marker', str(tmp_path / MARKER), '-Cases', str(cases)],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = dict(line.split('=') for line in proc.stdout.split())
    want = {c['id']: '1' if c['expect'] else '0' for c in RULE_CASES}
    want.update({f"env_{c['id']}": '1' if c['expect'] else '0' for c in ENV_CASES})
    assert got == want, proc.stderr

