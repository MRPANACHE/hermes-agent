"""Shell rewrite contracts independent of ownership-supervisor child count."""
from unittest.mock import MagicMock, patch
from tests.tools import test_process_registry as support

registry = support.registry
_reset_systemd_scope_cache = support._reset_systemd_scope_cache


class TestSpawnRewriteCompoundBackground:
    """Verify that spawn_local rewrites `A && B &` patterns to avoid subshell deadlocks.

    Issue #68915: when bash parses ``A && B &`` it forks a subshell ``(A && B) &``.
    If B is a long-running server, the subshell never exits and holds the stdout
    pipe open, causing a permanent deadlock. The rewriter wraps the tail to
    ``A && { B & }`` so no subshell fork occurs.
    """

    def test_compound_and_background_gets_rewritten(self, registry):
        """A && B & must be rewritten to A && { B & } before Popen."""
        captured_cmd = []

        def fake_popen(args, **kwargs):
            captured_cmd.append(args)
            proc = MagicMock()
            proc.pid = 1111
            proc.stdout = MagicMock()
            return proc

        fake_thread = MagicMock()
        fake_thread.daemon = False

        with patch("tools.process_registry._find_shell", return_value="/bin/bash"), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("threading.Thread", return_value=fake_thread), \
             patch.object(registry, "_write_checkpoint"):
            registry.spawn_local("cd /app && node server.js &>/tmp/srv.log &", cwd="/tmp")

        # De ownership-supervisor is een aparte spawn; dit contract gaat over
        # precies één shellcommando, niet over het aantal lifecycle-kinderen.
        shell_commands = [args for args in captured_cmd if args[:2] == ["/bin/bash", "-lic"]]
        assert len(shell_commands) == 1
        shell_cmd = shell_commands[0]
        # The command passed to Popen should be the REWRITTEN version
        assert "&& { node server.js &>/tmp/srv.log & }" in shell_cmd[2]


    def test_pty_path_uses_rewritten_command(self, registry):
        """PTY spawn path must also use the rewritten command (issue #68915)."""
        mock_pty_proc = MagicMock()
        mock_pty_proc.pid = 5555

        mock_pty_module = MagicMock()
        mock_pty_module.PtyProcess.spawn = MagicMock(return_value=mock_pty_proc)

        fake_thread = MagicMock()
        fake_thread.daemon = False

        with patch("tools.process_registry._find_shell", return_value="/bin/bash"), \
             patch.dict("sys.modules", {"ptyprocess": mock_pty_module}), \
             patch("threading.Thread", return_value=fake_thread), \
             patch.object(registry, "_write_checkpoint"):
            session = registry.spawn_local(
                "cd /app && node server.js &",
                cwd="/tmp",
                use_pty=True,
            )

        assert mock_pty_module.PtyProcess.spawn.called, \
            "PTY spawn should have been attempted"
        pty_args = mock_pty_module.PtyProcess.spawn.call_args[0][0]
        assert "&& { node server.js & }" in pty_args[2], \
            f"PTY path should use rewritten command, got: {pty_args[2]}"
        assert session.command == "cd /app && node server.js &"
