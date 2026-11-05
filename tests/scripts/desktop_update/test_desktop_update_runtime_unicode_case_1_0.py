"""Native Windows case within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_runtime_unicode import (CLI, HELPER, PROFILE, Path, _WRITE, _run_helper, publish_fixture_launcher, pytest)

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('code_page', [936])
def test_legacy_version_identity_check_matches_a_non_ascii_install(tmp_path: Path, code_page: int) -> None:
    """The user-bin fallback resolves by reported install directory, not PATH order.

    Red on base: the fixture CLI prints ``Install directory:`` as UTF-8 (the
    production CLI reconfigures stdio the same way), and Windows PowerShell
    5.1 decodes that capture with the console OEM code page — so under CP437
    a profile like ``Paweł Łącki`` reports mojibaked and the identity check's
    GetFullPath comparison never matches (#124526).
    """
    install = tmp_path / PROFILE / 'hermes-agent'
    launcher = publish_fixture_launcher(install, CLI)
    # Earlier PM installers published only to user-bin: move (not copy) the
    # launcher out of .hermes\bin so Get-HermesRuntimeCommand takes the
    # legacy branch. Its body still reports the checkout it was minted from.
    home = tmp_path / 'profile'
    userbin = home / 'bin'
    userbin.mkdir(parents=True)
    external = userbin / launcher.name
    launcher.rename(external)
    target = str(install).replace("'", "''")
    prefix = f"[Console]::OutputEncoding = [Text.Encoding]::GetEncoding({code_page}); "
    script = (prefix + f". '{HELPER}'; "
              f"try {{ $c = @(Get-HermesRuntimeCommand -InstallRoot '{target}') }} catch {{ exit 1 }}; "
              + _WRITE.format(lines="$c[0]"))
    lines = _run_helper(script, home, {}, tmp_path / 'answer.txt')
    assert lines[0] == str(external), (
        f"legacy identity check lost the non-ASCII path under code page {code_page}")

