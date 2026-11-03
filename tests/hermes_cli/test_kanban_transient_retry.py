"""Typed transient retries use native promotion/admission, never a side dispatcher."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.config import atomic_config_write
from hermes_cli.kanban_db_dispatch import _process_fingerprint
from tests.hermes_cli.test_kanban_hard_run_limits import board, card, hits


def blocked(conn, kind='transient'):
    tid = card(conn)
    with patch.object(kb.time, 'time', return_value=1000):
        run = kb.claim_task(conn, tid)
        assert run is not None
        assert kb.block_task(conn, tid, kind=kind, reason='fixture temporary failure',
                             expected_run_id=run.current_run_id)
    return tid, run.current_run_id


def promote(conn, now):
    with patch.object(kb.time, 'time', return_value=now):
        return kb.recompute_ready(conn)


def test_transient_cooldown_retry_is_idempotent_and_retains_history(board):
    tid, old = blocked(board)
    assert promote(board, 1059) == 0
    assert promote(board, 1060) == 1
    assert promote(board, 1061) == 0
    assert kb.get_task(board, tid).block_recurrences == 1
    assert board.execute('SELECT outcome FROM task_runs WHERE id=?', (old,)).fetchone()[0] == 'blocked'
    event = [e for e in kb.list_events(board, tid) if e.kind == 'promoted'][-1]
    assert event.payload['reason'] == 'transient_retry'
    assert event.payload['backoff_seconds'] == 60
    new = kb.claim_task(board, tid)
    assert new is not None and new.current_run_id != old
    assert kb.claim_task(board, tid) is None


@pytest.mark.parametrize('kind', ['needs_input', 'capability', None])
def test_nontransient_owner_blocks_remain_sticky(board, kind):
    tid, _ = blocked(board, kind)
    assert promote(board, 90000) == 0
    assert kb.get_task(board, tid).status == 'blocked'


def test_transient_recurrence_still_escalates_instead_of_looping(board):
    tid, _ = blocked(board)
    assert promote(board, 1060) == 1
    run = kb.claim_task(board, tid)
    assert run is not None
    assert kb.block_task(board, tid, kind='transient', expected_run_id=run.current_run_id)
    assert kb.get_task(board, tid).status == 'triage'
    assert promote(board, 90000) == 0


def test_transient_waits_for_the_previous_native_worker_to_exit(board):
    tid, run = blocked(board)
    with kb.write_txn(board):
        board.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                      (os.getpid(), _process_fingerprint(os.getpid()), run))
    assert promote(board, 1060) == 0
    with kb.write_txn(board):
        board.execute('UPDATE task_runs SET worker_pid=NULL,worker_started_at=NULL WHERE id=?', (run,))
    assert promote(board, 1060) == 1


def test_transient_preserves_parent_and_failure_breaker_gates(board):
    tid, _ = blocked(board)
    parent = card(board)
    kb.link_tasks(board, parent, tid)
    assert promote(board, 1060) == 0
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        board.execute('UPDATE tasks SET consecutive_failures=3 WHERE id=?', (tid,))
    assert promote(board, 1060) == 0
    assert kb.get_task(board, tid).status == 'blocked'


def test_retry_does_not_bypass_the_daily_budget(board):
    tid, _ = blocked(board)
    assert promote(board, 1060) == 1
    atomic_config_write(Path(os.environ['HERMES_HOME']) / 'config.yaml', {
        'kanban': {'hard_run_limits': True, 'daily_run_hours_overrides': {'2026-11-02': 40}}})
    usage = {'local_date': '2026-11-02', 'runtime_seconds': 40 * 3600, 'hours': 40,
             'timezone': 'Europe/Amsterdam', 'window_start': 0, 'window_end': 86400,
             'accounting_scope': 'all_native_boards', 'accounted_boards': ['fixture']}
    with patch('hermes_cli.kanban_db_run_limits.daily_usage', return_value=usage):
        assert kb.claim_task(board, tid) is None
    assert kb.get_task(board, tid).status == 'ready'
    assert hits(board, tid)[0][1]['threshold'] == 40
