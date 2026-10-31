"""Source binding of dispositions, not production activation evidence."""
import pytest
from unittest.mock import patch
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_run_limits import task_series, validate_supervisor_decision
from tests.hermes_cli.test_kanban_hard_run_limits import board, card, attempt, hits, disposition, deliver


def twenty_hours(conn):
    # Stable window, distinct cards to avoid consuming the per-card allowance.
    now = 1793484000  # overwritten below by ISO to keep intent inspectable
    from datetime import datetime
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    donor = card(conn)
    attempt(conn, donor, start=now - 20 * 3600, end=now)
    return now


@pytest.mark.parametrize("work_class", ["incident_work", "noor_day_work"])
def test_daily_exception_comes_from_canonical_supervisor_classification(board, work_class):
    now = twenty_hours(board)
    tid = kb.create_task(board, title="Not a class hint", assignee="noor" if work_class == "noor_day_work" else "owner")
    deliver(board, tid, disposition(board, tid, "classification", work_class=work_class))
    with patch("hermes_cli.kanban_db.time.time", return_value=now):
        assert kb.claim_task(board, tid) is not None
    _, p = hits(board, tid)[0]
    assert p["action"] == "exception_allowed"
    assert p["exception_class"] == work_class and p["exception_reason"]
    assert p["exception_event_id"] and p["evidence_refs"]


def test_title_profile_or_arbitrary_unblock_is_not_exception_authority(board):
    now = twenty_hours(board)
    tid = kb.create_task(board, title="INCIDENT Noor daily work", assignee="noor")
    with patch("hermes_cli.kanban_db.time.time", return_value=now):
        assert kb.claim_task(board, tid) is None
    assert hits(board, tid)[0][1]["action"] == "blocked_new_run"


def test_exception_never_bypasses_five_attempt_cap(board):
    tid = card(board)
    deliver(board, tid, disposition(board, tid, "classification", work_class="incident_work"))
    for _ in range(5):
        attempt(board, tid)
    assert kb.claim_task(board, tid) is None


def test_measured_linked_closure_resets_only_assessed_execution_prefix(board):
    tid = card(board)
    for _ in range(5):
        attempt(board, tid)
    assert kb.claim_task(board, tid) is None
    d = disposition(board, tid, "measured_closure", case_ref="oos://cases/fixture", measurement_ref="oos://measurements/fixture")
    assert validate_supervisor_decision(board, tid, d) == d
    deliver(board, tid, d)
    assert task_series(board, tid)["run_ids"] == []
    assert kb.unblock_task(board, tid)
    attempt(board, tid)
    assert len(task_series(board, tid)["run_ids"]) == 1


@pytest.mark.parametrize("field,value", [("task_id", "t_deadbeef"), ("origin_event_id", 0),
    ("run_ids", []), ("measurement_ref", "oos://missing"), ("reason", ""), ("evidence_refs", [])])
def test_invalid_measurement_or_cross_task_decision_rejected(board, field, value):
    tid = card(board)
    attempt(board, tid)
    d = disposition(board, tid, "measured_closure", case_ref="oos://cases/fixture", measurement_ref="oos://measurements/fixture")
    d[field] = value
    with pytest.raises(ValueError, match="binding_invalid"):
        validate_supervisor_decision(board, tid, d)


def test_copied_actor_string_without_oos_delivery_does_not_reset_series(board):
    tid = card(board)
    attempt(board, tid)
    d = disposition(board, tid, "measured_closure", case_ref="oos://cases/fixture", measurement_ref="oos://measurements/fixture")
    deliver(board, tid, d, actor_via="pasted-note")
    assert len(task_series(board, tid)["run_ids"]) == 1


def test_missing_native_delivery_pair_is_not_authority(board):
    tid = card(board)
    attempt(board, tid)
    d = disposition(board, tid, "measured_closure", case_ref="oos://cases/fixture", measurement_ref="oos://measurements/fixture")
    deliver(board, tid, d)
    with kb.write_txn(board):
        board.execute("DELETE FROM task_events WHERE task_id=? AND kind='oos_decision_delivery'", (tid,))
    assert len(task_series(board, tid)["run_ids"]) == 1


def test_noor_day_work_requires_native_noor_assignee(board):
    tid = card(board)
    with pytest.raises(ValueError, match="binding_invalid"):
        validate_supervisor_decision(board, tid, disposition(board, tid, "classification", work_class="noor_day_work"))


def test_later_supervisor_regular_classification_revokes_exception(board):
    now = twenty_hours(board)
    tid = card(board)
    deliver(board, tid, disposition(board, tid, "classification", work_class="incident_work"))
    deliver(board, tid, disposition(board, tid, "classification", work_class="regular_work"))
    with patch("hermes_cli.kanban_db.time.time", return_value=now):
        assert kb.claim_task(board, tid) is None
