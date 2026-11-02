"""An exact-date operator budget never becomes a permanent cap bypass."""
from unittest.mock import patch
from pathlib import Path
import os
import pytest
from hermes_cli.config import atomic_config_write
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_run_limits import enforce_run_limits
from tests.hermes_cli.test_kanban_hard_run_limits import board, card, hits


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
