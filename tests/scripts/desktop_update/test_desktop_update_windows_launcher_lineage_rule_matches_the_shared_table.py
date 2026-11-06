"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_handoff_lineage import (ENV_CASES, MARKER, MARKER_PS1, POWERSHELL, Path, RULE_CASES, _PASCAL, _RULE_HARNESS, json, pytest, subprocess)

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
