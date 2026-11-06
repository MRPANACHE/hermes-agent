"""Session turns and spawned card runs spend the same Amsterdam day budget."""
import json
from datetime import datetime

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_run_limits import daily_usage, day_window, enforce_run_limits
from tests.hermes_cli import test_kanban_hard_run_limits as limit_fixtures
from tests.hermes_cli.test_kanban_hard_run_limits import hits
from tests.hermes_cli.test_kanban_assignee_day_share import configure, spent

board = limit_fixtures.board


def ledger(rows):
    path = kb.kanban_home() / 'kanban' / 'session-usage.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def turn(profile, start, end, agent='noor'):
    return dict(profile=profile, agent=agent, started_at=start, ended_at=end, log='/fixture/log')


@pytest.mark.parametrize('date,day_hours', [('2026-11-05', 24), ('2026-03-29', 23), ('2026-10-25', 25)])
def test_shared_ledger_clips_to_amsterdam_day_and_now(board, date, day_hours):
    now = int(datetime.fromisoformat(date + 'T12:00:00+01:00').timestamp())
    window = day_window(now)
    start, end = window['start'], window['end']
    assert end - start == day_hours * 3600
    spent(board, 'otto', 1, now)
    ledger([turn('otto', start - 3600, start + 60),
            turn('mara', now - 90, now + 1000),
            turn('noor', now + 1, end + 100),
            turn('otto', start - 100, start),
            turn('otto', end, end + 100)])
    usage = daily_usage(board, now)
    assert usage['runtime_seconds'] == 3600 + 60 + 90
    assert usage['hours'] == (3600 + 60 + 90) / 3600
    assert daily_usage(board, now, 'otto')['runtime_seconds'] == 3660
    # The executing profile spends its share, not the session's requesting agent.
    assert daily_usage(board, now, 'mara')['runtime_seconds'] == 90
    assert daily_usage(board, now, 'noor')['runtime_seconds'] == 0
    assert daily_usage(board, end)['runtime_seconds'] == 0
    assert daily_usage(board, end + 100)['runtime_seconds'] == 200


def test_sessions_exhaust_fleet_and_profile_admission(board):
    now = day_window(int(datetime.fromisoformat('2026-11-05T12:00:00+01:00').timestamp()))['start'] + 12 * 3600
    configure({'otto': 25})
    otto = kb.create_task(board, title='machinery', assignee='otto')
    noor = kb.create_task(board, title='business', assignee='noor')
    ledger([turn('otto', now - 5 * 3600, now)])
    assert enforce_run_limits(board, otto, 'ready', now) is False
    assert enforce_run_limits(board, noor, 'ready', now) is True
    assert hits(board, otto)[0][1]['limit_key'] == 'assignee_day_share'
    ledger([turn('otto', now - 5 * 3600, now), turn('mara', now - 10 * 3600, now),
            turn('noor', now - 5 * 3600, now)])
    assert enforce_run_limits(board, noor, 'ready', now) is False
    assert hits(board, noor)[0][1]['observed'] == 20
    assert kb.get_task(board, noor).status == 'ready'
    assert enforce_run_limits(board, noor, 'ready', day_window(now)['end']) is True


def test_absent_ledger_is_zero_but_unreadable_accounting_never_grants_budget(board):
    now = 1793876400
    assert daily_usage(board, now)['runtime_seconds'] == 0
    path = ledger([])
    path.write_text('{"profile":')
    with pytest.raises(ValueError):
        daily_usage(board, now)
    path.unlink()
    path.mkdir()
    with pytest.raises(OSError):
        daily_usage(board, now)
