"""Native Windows handoff cells isolated within the unchanged per-file deadline."""
from __future__ import annotations

from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import (
    Path,
    _TIMED_CHECKOUT_HOLDER,
    _handoff,
    pytest,
    subprocess,
    sys,
    time,
)


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    holders = []
    release = tmp_path / 'release-holder'
    ready = tmp_path / 'holder-ready'
    observed = tmp_path / 'r6-observed'

    def hold_checkout(_home: Path, install: Path) -> None:
        proc = subprocess.Popen([sys.executable, '-c', _TIMED_CHECKOUT_HOLDER,
                                 str(install / '.hermes-update.lock'), str(ready), str(release)])
        holders.append(proc)
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, 'checkout holder did not acquire its lock'
            time.sleep(0.05)

    import threading

    def release_after_wait() -> None:
        log = tmp_path / 'home/logs/desktop-update-handoff.log'
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if log.exists() and 'keeping the update marker' in log.read_text(encoding='utf-8-sig'):
                observed.touch()
                release.touch()
                return
            time.sleep(0.05)
        release.touch()

    monitor = threading.Thread(target=release_after_wait, daemon=True)
    try:
        monitor.start()
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
    finally:
        release.touch()
        if holders:
            holders[0].wait(timeout=10)
        monitor.join(timeout=5)
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert observed.exists(), 'the test released the holder before observing R6 wait'
    assert result['ok'] is True and result['finished_at'] >= int(observed.stat().st_mtime), result


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)


@pytest.mark.platforms('windows')
def test_watchdog_remap_after_banner_reports_interrupted_followups(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_HANG='Update complete! (v1.0.0)',
        HERMES_UPDATE_STEP_IDLE_SECONDS='3',
    )
    assert code == 0, out
    assert (result['ok'], result['manual']) == (True, True), result
    assert any('interrupted' in w for w in result['warnings']), result


@pytest.mark.platforms('windows')
def test_cpu_busy_pipe_silent_update_is_not_killed_by_the_idle_watchdog(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_BUSY_SECONDS='15', HERMES_UPDATE_STEP_IDLE_SECONDS='4',
    )
    assert code == 0, out
    assert (result['ok'], result['warnings']) == (True, []), result


@pytest.mark.platforms('windows')
def test_hanging_update_help_probe_is_bounded(tmp_path: Path) -> None:
    code, out, argv, _result, _ = _handoff(
        tmp_path, '-NoGateway', '-ProbeTimeoutSeconds', '10', timeout=120, HANDOFF_HELP_HANG='1',
    )
    assert code == 0, out
    assert argv[0][:2] == ['update', '--yes'] and '--keep-stash' not in argv[0], argv
