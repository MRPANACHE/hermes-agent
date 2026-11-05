"""Native Windows case within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_runtime_unicode import (CLI, HELPER, PROFILE, Path, _WRITE, _run_helper, json, publish_fixture_launcher, pytest, sys)

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('code_page', [437])
def test_runtime_command_survives_a_non_ascii_profile_path(tmp_path: Path, code_page: int) -> None:
    """Get-HermesRuntimeCommand returns the launcher's exact interpreter path.

    The launcher JSON is ASCII-escaped today, so this is the round-trip
    contract for the boundary every other consumer (hermes_cli.windows_ssh_
    runtime, the Electron updater, the Rust bootstrap) already decodes as
    UTF-8: the capture must return the path byte-exact and leave the caller's
    console encoding unchanged, whatever code page the console carries.
    """
    profile = tmp_path / PROFILE
    install = profile / 'hermes-agent'
    # PM's store under the same non-ASCII profile, with a facts.json naming a
    # store interpreter there — the layout a real install resolves and prints.
    store = profile / 'tools'
    python_dir = store / 'cpython-3.14-windows-x86_64-none'
    python_dir.mkdir(parents=True)
    python = python_dir / 'python.exe'
    python.write_bytes(Path(sys.executable).read_bytes())
    (store / 'facts.json').write_text(
        json.dumps({'packages': {'python': {'entry': python_dir.name}}}), encoding='utf-8')
    publish_fixture_launcher(install, CLI)
    home = tmp_path / 'home'
    home.mkdir()
    target = str(install).replace("'", "''")
    prefix = f"[Console]::OutputEncoding = [Text.Encoding]::GetEncoding({code_page}); "
    script = (prefix + f". '{HELPER}'; "
              f"$c = @(Get-HermesRuntimeCommand -InstallRoot '{target}'); "
              + _WRITE.format(lines="$c[0], [Console]::OutputEncoding.CodePage"))
    lines = _run_helper(script, home, {'HERMES_RUNTIME_DIR': str(store)}, tmp_path / 'answer.txt')
    assert lines[0] == str(python), (
        f"interpreter path was mojibaked under code page {code_page}: {lines[0]!r}")
    assert int(lines[1]) == code_page, 'the capture must leave the caller console encoding unchanged'

