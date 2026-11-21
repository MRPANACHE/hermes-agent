"""An exact-date operator budget never becomes a permanent cap bypass."""
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.config import atomic_config_replace, atomic_config_write
from hermes_cli.kanban_db_run_limits import daily_usage, enforce_run_limits
from tests.hermes_cli import test_kanban_hard_run_limits as limits

board = limits.board
card = limits.card
hits = limits.hits
NOON = int(datetime.fromisoformat('2026-11-21T12:00:00+01:00').timestamp())


@pytest.mark.parametrize('date,hours,allowed', [
    ('2026-11-02', 39, True), ('2026-11-02', 40, False),
    ('2026-11-03', 21, False), ('2026-11-01', 21, False),
])
def test_exact_day_budget_expires_and_keeps_ready_cards(board, date, hours, allowed):
    tid = card(board)
    atomic_config_write(Path(os.environ['HERMES_HOME']) / 'config.yaml', {
        'kanban': {'hard_run_limits': True, 'daily_run_hours_overrides': {'2026-11-02': 40}}})
    usage = {'local_date': date, 'runtime_seconds': hours * 3600, 'hours': hours,
             'timezone': 'Europe/Amsterdam', 'window_start': 0, 'window_end': 86400,
             'accounting_scope': 'all_native_boards', 'accounted_boards': ['fixture']}
    with patch('hermes_cli.kanban_db_run_limits.daily_usage', return_value=usage):
        assert enforce_run_limits(board, tid, 'ready', 1) is allowed
    assert kb.get_task(board, tid).status == 'ready'
    if not allowed:
        assert hits(board, tid)[0][1]['threshold'] == (40 if date == '2026-11-02' else 20)


@pytest.mark.parametrize('cap,overrides,expected', [
    (60, {}, 60),
    (42, {'2026-11-20': 39}, 42),
    (60, {'2026-11-21': 39}, 39),
    (60, {'2026-11-21': True}, 60),
    (60, False, 60),
    (None, {}, 20),
    (True, {}, 20),
    (0, {}, 20),
    (-4, {}, 20),
    ('60', {}, 20),
    (60.5, {}, 20),
])
def test_native_claim_counts_cards_and_sessions_at_effective_cap(board, cap, overrides, expected):
    now = NOON
    home = Path(os.environ['HERMES_HOME'])
    settings = {'hard_run_limits': True, 'daily_run_hours_overrides': overrides}
    if cap is not None:
        settings['daily_run_hours'] = cap
    atomic_config_replace(home / 'config.yaml', {'kanban': settings})

    start = now - 3600
    # One hour of native card work, plus overlapping turns in the shared ledger.
    limits.attempt(board, card(board), start=start, end=now)
    ledger = home / 'kanban' / 'session-usage.jsonl'
    ledger.parent.mkdir(exist_ok=True)
    rows = [{'started_at': start, 'ended_at': now, 'profile': 'owner'} for _ in range(expected - 1)]
    rows[-1]['ended_at'] -= 1
    ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    with patch.object(kb.time, 'time', return_value=now):
        admitted = card(board)
        assert daily_usage(board, now)['runtime_seconds'] == expected * 3600 - 1
        assert kb.claim_task(board, admitted) is not None

        rows[-1]['ended_at'] += 1
        ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        denied = card(board)
        assert kb.claim_task(board, denied) is None

    task = kb.get_task(board, denied)
    assert task is not None and task.status == 'ready'
    hit = hits(board, denied)[0][1]
    assert hit is not None
    assert (hit['limit_key'], hit['observed'], hit['threshold'], hit['action']) == (
        'daily_run_hours', expected, expected, 'blocked_new_run')


def test_permanent_cap_isolated_to_dispatcher_profile(board, tmp_path, monkeypatch):
    config = Path(os.environ['HERMES_HOME']) / 'config.yaml'
    other = tmp_path / 'profiles' / 'other'
    other.mkdir(parents=True)
    atomic_config_write(config, {'kanban': {'hard_run_limits': True, 'daily_run_hours': 60}})
    atomic_config_write(other / 'config.yaml', {'kanban': {'hard_run_limits': True}})
    now = NOON
    ledger = tmp_path / 'kanban' / 'session-usage.jsonl'
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text(''.join(json.dumps({'started_at': now - 3600, 'ended_at': now, 'profile': 'owner'}) + '\n'
                              for _ in range(20)), encoding='utf-8')

    with patch.object(kb.time, 'time', return_value=now):
        assert kb.claim_task(board, card(board)) is not None
        monkeypatch.setenv('HERMES_HOME', str(other))
        denied = card(board)
        assert kb.claim_task(board, denied) is None
        hit = hits(board, denied)[0][1]
        assert hit is not None and hit['threshold'] == 20
