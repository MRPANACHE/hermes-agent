"""A failed taskkill tree command is not success without independent target exit."""
from types import SimpleNamespace

import psutil
import pytest

from gateway.status_taskkill import require_exit_after_taskkill_error


@pytest.mark.parametrize('state', ['exited', 'exited_without_code', 'gone', 'running', 'denied', 'unknown'])
def test_taskkill_error_requires_a_terminal_target_observation(monkeypatch, state):
    checked = []

    class Process:
        def __init__(self, pid):
            checked.append(pid)
            if state == 'gone':
                raise psutil.NoSuchProcess(pid)

        def wait(self, timeout):
            assert timeout == 0
            if state == 'running':
                raise psutil.TimeoutExpired(timeout)
            if state == 'denied':
                raise psutil.AccessDenied(1234)
            if state == 'unknown':
                raise psutil.Error('unavailable observation')
            return None if state == 'exited_without_code' else 0

    monkeypatch.setattr(psutil, 'Process', Process)
    result = SimpleNamespace(returncode=1, stdout='', stderr='original taskkill error')
    if state in ('exited', 'exited_without_code', 'gone'):
        assert require_exit_after_taskkill_error(1234, result) is None
    else:
        with pytest.raises(OSError, match='original taskkill error'):
            require_exit_after_taskkill_error(1234, result)
    assert checked == [1234]
