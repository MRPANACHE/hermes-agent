"""A profile's day share leaves the rest of the fleet budget to the other agents."""
import os
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.config import atomic_config_replace as atomic_config_write
from hermes_cli.kanban_db_run_limits import day_window, enforce_run_limits
from tests.hermes_cli import test_kanban_hard_run_limits as limits

board = limits.board


def configure(shares):
    atomic_config_write(Path(os.environ['HERMES_HOME']) / 'config.yaml', {
        'kanban': {'hard_run_limits': True, 'daily_run_share_per_assignee': shares}})


def spent(conn, assignee, hours, now):
    tid = kb.create_task(conn, title=f'{assignee} earlier work', assignee=assignee)
    run = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'spawned', {'pid': 999999999}, run_id=run.current_run_id)
        conn.execute('UPDATE task_runs SET started_at=?, ended_at=? WHERE id=?',
                     (now - int(hours * 3600), now, run.current_run_id))
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL, worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,))


def noon():
    import time
    return day_window(int(time.time()))['start'] + 12 * 3600


def test_capped_profile_waits_while_others_still_run(board):
    now = noon()
    configure({'otto': 25})
    spent(board, 'otto', 5, now)  # 25 % of the default 20 h
    otto = kb.create_task(board, title='machinery', assignee='otto')
    noor = kb.create_task(board, title='business', assignee='noor')
    assert enforce_run_limits(board, otto, 'ready', now) is False
    assert enforce_run_limits(board, noor, 'ready', now) is True
    hit = limits.hits(board, otto)[0][1]
    assert (hit['limit_key'], hit['assignee'], hit['threshold'], hit['action']) == (
        'assignee_day_share', 'otto', 5.0, 'blocked_new_run')
    assert kb.get_task(board, otto).status == 'ready'


def test_under_share_and_invalid_entries_admit(board):
    now = noon()
    spent(board, 'otto', 4.9, now)
    otto = kb.create_task(board, title='machinery', assignee='otto')
    configure({'otto': 25})
    assert enforce_run_limits(board, otto, 'ready', now) is True
    for bad in ({'otto': '25'}, {'otto': 0}, {'otto': 100}, {'otto': 2.5}, ['otto']):
        configure(bad)
        assert enforce_run_limits(board, otto, 'ready', now) is True


def test_share_follows_the_dated_budget(board):
    now = noon()
    spent(board, 'otto', 6, now)
    otto = kb.create_task(board, title='machinery', assignee='otto')
    date = day_window(now)['local_date']
    atomic_config_write(Path(os.environ['HERMES_HOME']) / 'config.yaml', {
        'kanban': {'hard_run_limits': True, 'daily_run_hours_overrides': {date: 40},
                   'daily_run_share_per_assignee': {'otto': 25}}})
    assert enforce_run_limits(board, otto, 'ready', now) is True  # 6 h < 10 h
