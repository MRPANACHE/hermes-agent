"""Native admission-kernel tests on disposable boards, not production evidence."""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.config import atomic_config_write
from hermes_cli.kanban_db_run_limits import daily_usage, day_window, task_series


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    atomic_config_write(tmp_path / "config.yaml", {"kanban": {"hard_run_limits": True}})
    conn = kb.connect()
    yield conn
    conn.close()


def card(conn):
    return kb.create_task(conn, title="Fixture worker task", assignee="owner")


def attempt(conn, tid, *, start=None, end=None):
    run = kb.claim_task(conn, tid)
    assert run is not None
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "spawned", {"pid": 999999999}, run_id=run.current_run_id)
    assert kb.block_task(conn, tid, kind="needs_input", reason="fixture checkpoint", expected_run_id=run.current_run_id)
    assert kb.unblock_task(conn, tid)
    if start is not None:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET started_at=?,ended_at=? WHERE id=?", (start, end, run.current_run_id))
    return run.current_run_id


def hits(conn, tid):
    return [(e, e.payload) for e in kb.list_events(conn, tid) if e.kind == "limit_hit"]


def disposition(conn, tid, kind, **extras):
    from hermes_cli.kanban_db_run_limits import execution_runs
    return {"schema": "mrpanache.run-limit-decision.v1", "task_id": tid,
            "origin_event_id": kb.list_events(conn, tid)[0].id,
            "run_ids": [r["id"] for r in execution_runs(conn, task_id=tid)],
            "kind": kind, "reason": "Fixture supervisor assessment",
            "evidence_refs": ["oos://cases/fixture", "oos://measurements/fixture"], **extras}


def deliver(conn, tid, decision, **overrides):
    """Fixture canonical delivery pair; integration tests exercise the producer."""
    with kb.write_txn(conn):
        request_id = "fixture:" + str(kb.list_events(conn, tid)[-1].id)
        context = {"decision_id": request_id, "revision_sha256": "a" * 64,
                   "answered_by": "mara", "delivered_by": "oos_decision_delivery"}
        payload = {"request_id": request_id, "actor_requested_by": "agent:mara", "actor_via": "oos-decisions",
                   "actor_origin_task_id": tid, "oos_delivery": context,
                   "answer": {"run_limit_decision": decision}, **overrides}
        kb._append_event(conn, tid, "mrpanache_resumed", payload)
        native_id = kb.list_events(conn, tid)[-1].id
        kb._append_event(conn, tid, "oos_decision_delivery", {"request_id": request_id,
                         "receipt": {**context, "native_receipt": {"event_id": native_id}}})
    return native_id



def test_card_attempts_never_require_a_supervisor_reset(board):
    tid = card(board)
    ids = [attempt(board, tid) for _ in range(12)]
    assert len(ids) == 12
    assert kb.claim_task(board, tid) is not None
    assert hits(board, tid) == []


def test_administrative_schedule_rows_do_not_spend_attempts(board):
    tid = card(board)
    with kb.write_txn(board):
        for _ in range(8):
            board.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at) VALUES(?,?,'scheduled','scheduled',1,1)", (tid, "owner"))
    assert task_series(board, tid)["run_ids"] == []
    assert daily_usage(board, 100000)["runtime_seconds"] == 0
    assert kb.claim_task(board, tid) is not None


def test_review_lane_has_no_card_attempt_stop(board):
    tid = card(board)
    for _ in range(6):
        attempt(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    assert kb.claim_review_task(board, tid) is not None
    assert hits(board, tid) == []


def test_wait_only_is_diagnostic_but_keeps_daily_runtime(board):
    now = int(datetime.fromisoformat("2026-10-31T12:00:00+01:00").timestamp())
    tid = card(board)
    task = kb.claim_task(board, tid)
    with kb.write_txn(board):
        kb._append_event(board, tid, "spawned", {"pid": 999999998}, run_id=task.current_run_id)
        board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now-600, task.current_run_id))
    with patch("hermes_cli.kanban_db.time.time", return_value=now):
        assert kb.schedule_task(board, tid, reason="wait only", wait_only=True, expected_run_id=task.current_run_id)
    assert task_series(board, tid)["run_ids"] == []
    assert daily_usage(board, now)["runtime_seconds"] == 600


def test_native_completion_resets_series_but_comment_does_not(board):
    tid = card(board)
    attempt(board, tid)
    kb.add_comment(board, tid, "owner", "fixed; a PR was made")
    assert len(task_series(board, tid)["run_ids"]) == 1
    assert kb.complete_task(board, tid, summary="Fixture measurable task completion")
    assert task_series(board, tid)["run_ids"] == []


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25), ("2026-10-31", 24)])
def test_amsterdam_calendar_midnights_and_dst(day, hours):
    now = int(datetime.fromisoformat(day + "T12:00:00+01:00").timestamp())
    window = day_window(now)
    assert window["local_date"] == day
    assert window["end"] - window["start"] == hours * 3600


def test_daily_hours_clip_overnight_and_running_intervals(board):
    now = int(datetime.fromisoformat("2026-10-31T12:00:00+01:00").timestamp())
    midnight = day_window(now)["start"]
    tid = card(board)
    attempt(board, tid, start=midnight - 3600, end=midnight + 2 * 3600)
    running = card(board)
    run = kb.claim_task(board, running)
    with kb.write_txn(board):
        kb._append_event(board, running, "spawned", {"pid": 999999998}, run_id=run.current_run_id)
        board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now - 3600, run.current_run_id))
    assert daily_usage(board, now)["runtime_seconds"] == 3 * 3600


def test_twenty_hours_stop_new_ready_and_review_leave_running_untouched(board):
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    tid = card(board)
    attempt(board, tid, start=now - 20 * 3600, end=now)
    ready, review, active = card(board), card(board), card(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
    # A run already admitted before the cap remains unchanged.
    with patch("hermes_cli.config.load_config_readonly", return_value={"kanban": {"hard_run_limits": False}}):
        running = kb.claim_task(board, active)
    with patch("hermes_cli.kanban_db.time.time", return_value=now):
        assert kb.claim_task(board, ready) is None
        assert kb.claim_review_task(board, review) is None
        assert kb.claim_task(board, ready) is None
    assert kb.get_task(board, active).current_run_id == running.current_run_id
    assert kb.get_task(board, active).status == "running"
    assert kb.get_task(board, ready).status == "ready"
    assert kb.get_task(board, review).status == "review"
    assert kb.list_runs(board, ready) == []
    assert len(hits(board, ready)) == 1
    _, p = hits(board, ready)[0]
    assert p["observed"] == p["threshold"] == 20
    assert p["window_start"] == "2026-10-31T00:00:00+01:00"
    assert p["window_end"] == "2026-11-01T00:00:00+01:00"
    with patch("hermes_cli.kanban_db.time.time", return_value=now + 2 * 3600):
        assert kb.claim_task(board, ready) is not None


def alarm_config(**limit):
    return {"kanban": {"hard_run_limits": True, "alarm_run_limit": limit or {"created_by": ["alarm-bot"], "daily_hours": 4}}}


def alarm_card(conn):
    return kb.create_task(conn, title="Fixture alarm card", assignee="owner", created_by="alarm-bot")


def claim_at(conn, tid, now, config):
    with patch("hermes_cli.config.load_config_readonly", return_value=config), \
            patch("hermes_cli.kanban_db.time.time", return_value=now):
        return kb.claim_task(conn, tid)


def test_alarm_card_starts_above_a_spent_day_budget_and_ordinary_cards_do_not(board):
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    attempt(board, card(board), start=now - 21 * 3600, end=now)
    ordinary, alarm = card(board), alarm_card(board)
    assert claim_at(board, ordinary, now, alarm_config()) is None
    assert claim_at(board, alarm, now, alarm_config()) is not None
    _, payload = hits(board, alarm)[0]
    assert (payload["limit_key"], payload["action"], payload["exception_class"]) == (
        "daily_run_hours", "exception_allowed", "alarm_card")
    assert (payload["observed"], payload["threshold"], payload["alarm_threshold"]) == (21, 20, 4)
    assert hits(board, ordinary)[0][1]["action"] == "blocked_new_run"


def test_alarm_cards_stop_at_their_own_cap_even_with_day_budget_left(board):
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    attempt(board, alarm_card(board), start=now - 4 * 3600, end=now)
    assert claim_at(board, alarm_card(board), now - 1, alarm_config()) is not None
    waiting = alarm_card(board)
    assert claim_at(board, waiting, now, alarm_config()) is None
    assert claim_at(board, waiting, now, alarm_config()) is None
    assert kb.get_task(board, waiting).status == "ready"
    assert len(hits(board, waiting)) == 1
    _, payload = hits(board, waiting)[0]
    assert (payload["limit_key"], payload["action"], payload["observed"], payload["threshold"]) == (
        "daily_alarm_run_hours", "blocked_new_run", 4, 4)
    assert claim_at(board, card(board), now, alarm_config()) is not None
    assert claim_at(board, waiting, now + 2 * 3600, alarm_config()) is not None


def test_ordinary_runtime_does_not_spend_the_alarm_cap_but_alarm_runtime_counts_for_everyone(board):
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    attempt(board, card(board), start=now - 17 * 3600, end=now)
    attempt(board, alarm_card(board), start=now - 3 * 3600, end=now)
    ledger = Path(board.execute("PRAGMA database_list").fetchone()[2]).parent / "kanban" / "session-usage.jsonl"
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text(json.dumps({"profile": "owner", "started_at": now - 3600, "ended_at": now}) + "\n")
    assert daily_usage(board, now, creators=frozenset({"alarm-bot"}))["runtime_seconds"] == 3 * 3600
    assert claim_at(board, card(board), now, alarm_config()) is None
    assert claim_at(board, alarm_card(board), now, alarm_config()) is not None


@pytest.mark.parametrize("limit", [
    {"created_by": ["alarm-bot"]}, {"daily_hours": 4}, {"created_by": [], "daily_hours": 4},
    {"created_by": "alarm-bot", "daily_hours": 4}, {"created_by": [""], "daily_hours": 4},
    {"created_by": ["alarm-bot"], "daily_hours": 0}, {"created_by": ["alarm-bot"], "daily_hours": 21},
    {"created_by": ["alarm-bot"], "daily_hours": "4"}, {"created_by": ["alarm-bot"], "daily_hours": True},
])
def test_invalid_alarm_setting_never_lifts_the_day_budget(board, limit):
    now = int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())
    attempt(board, card(board), start=now - 20 * 3600, end=now)
    alarm = alarm_card(board)
    assert claim_at(board, alarm, now, alarm_config(**limit)) is None
    assert hits(board, alarm)[0][1]["limit_key"] == "daily_run_hours"
