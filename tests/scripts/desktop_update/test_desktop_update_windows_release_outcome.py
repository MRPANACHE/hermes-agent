"""Native Windows contracts split at behavior boundaries; unchanged per-file timeout."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import _handoff
from tests.scripts.desktop_update.test_desktop_update_windows_marker_ops import _CHECKOUT_HOLDER
from tests.scripts.desktop_update.windows_handoff_support import _wait_for_log
from tests.waiting import wait_until

@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    holders, releases, failures = [], [], []
    release = tmp_path / 'release-checkout'
    observer = None

    def release_after_wait(home: Path) -> None:
        try:
            _wait_for_log(home, 'keeping the update marker')
            releases.append(time.time())
        except BaseException as exc:
            failures.append(exc)
        finally:
            release.touch()

    def hold_checkout(home: Path, install: Path) -> None:
        nonlocal observer
        holders.append(subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER,
                                         str(install / '.hermes-update.lock'), str(release)]))
        wait_until(lambda: Path(str(release) + '.ready').exists(), timeout=30,
                   what='checkout holder acquired the native lock')
        observer = threading.Thread(target=release_after_wait, args=(home,), daemon=True)
        observer.start()
    try:
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
    finally:
        release.touch()
        for proc in holders:
            proc.wait(timeout=30)
        if observer is not None:
            observer.join(timeout=95)
    assert not failures, failures
    assert observer is not None and not observer.is_alive()
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert releases, 'release must follow the actual R6 wait, not a guessed startup duration'
    assert result['ok'] is True and result['finished_at'] >= int(releases[0]), result


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
