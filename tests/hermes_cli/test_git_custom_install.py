"""Git installation layout is data; candidate probes still reject broken binaries."""
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.update_cmd_git import _locate_real_git


def test_git_core_is_found_under_resolved_custom_install(tmp_path):
    root = tmp_path / "managed-git"
    shim = root / "cmd" / "git.exe"
    real = root / "mingw64" / "libexec" / "git-core" / "git.exe"
    shim.parent.mkdir(parents=True)
    real.parent.mkdir(parents=True)
    shim.touch()
    real.touch()
    with patch("hermes_platform.resolver.locate_command", return_value=SimpleNamespace(command=(str(shim),))), \
            patch("hermes_cli.update_cmd_git._probe_fork_bomb", return_value=False) as probe:
        assert _locate_real_git() == real
        assert [str(real)] in [call.args[0] for call in probe.call_args_list]
    with patch("hermes_platform.resolver.locate_command", return_value=SimpleNamespace(command=(str(shim),))), \
            patch("hermes_cli.update_cmd_git._probe_fork_bomb", return_value=True):
        # No real candidate is admitted merely because it exists.
        assert _locate_real_git() is None
