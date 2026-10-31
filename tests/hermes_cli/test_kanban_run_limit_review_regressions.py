"""Regressions for the two source-review findings; no production mutations."""
from contextlib import nullcontext
from datetime import datetime
import json
import sqlite3
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_run_limits import daily_usage, supervisor_decisions
from tools import kanban_tools as kt
from tests.hermes_cli.test_kanban_hard_run_limits import board, attempt, card, disposition, hits


def now():
    return int(datetime.fromisoformat("2026-10-31T23:00:00+01:00").timestamp())


def other_board(root, slug="other", archived=False):
    path = root / "kanban" / "boards"
    path = path / "_archived" / (slug + "-retained") if archived else path / slug
    path.mkdir(parents=True, exist_ok=True)
    if not archived:
        kb.write_board_metadata(slug, name=slug)
    return kb.connect(path / "kanban.db")


def native_decision(conn, tid, decision, identity="mara"):
    # Exercise the existing native tool handler, not a manually fabricated event.
    with patch.object(kt, "_board", return_value=nullcontext((kb, conn))), \
            patch.object(kt, "_persisted_identity", return_value=identity):
        return json.loads(kt._handle_comment({"task_id": tid, "body": json.dumps(decision)}))


def test_twenty_hours_is_shared_across_boards_despite_worker_db_pin(board, tmp_path):
    a = card(board)
    other = other_board(tmp_path)
    try:
        b = card(other)
        attempt(board, a, start=now() - 7 * 3600, end=now())
        attempt(other, b, start=now() - 13 * 3600, end=now())
        usage = daily_usage(board, now())
        assert usage["runtime_seconds"] == 20 * 3600
        assert usage["accounting_scope"] == "shared_boards"
        assert len(usage["accounted_boards"]) == 2
        candidate = card(board)
        before = len(kb.list_runs(board, candidate))
        with patch("hermes_cli.kanban_db.time.time", return_value=now()):
            assert kb.claim_task(board, candidate) is None
        assert len(kb.list_runs(board, candidate)) == before
        assert hits(board, candidate)[0][1]["observed"] == 20
        assert hits(board, candidate)[0][1]["action"] == "blocked_new_run"
        assert daily_usage(other, now())["runtime_seconds"] == 20 * 3600
    finally:
        other.close()


def test_other_board_alone_exhausts_daily_budget(board, tmp_path):
    other = other_board(tmp_path)
    try:
        attempt(other, card(other), start=now() - 20 * 3600, end=now())
        candidate = card(board)
        with patch("hermes_cli.kanban_db.time.time", return_value=now()):
            assert kb.claim_task(board, candidate) is None
        assert hits(board, candidate)[0][1]["observed"] == 20
    finally:
        other.close()


def test_default_board_alias_does_not_double_count_pinned_database(board, tmp_path):
    attempt(board, card(board), start=now() - 11 * 3600, end=now())
    (tmp_path / "kanban.db").symlink_to(tmp_path / "board.db")
    assert daily_usage(board, now())["runtime_seconds"] == 11 * 3600
    assert len(daily_usage(board, now())["accounted_boards"]) == 1


def test_retained_archived_board_keeps_todays_usage(board, tmp_path):
    other = other_board(tmp_path, archived=True)
    try:
        attempt(other, card(other), start=now() - 20 * 3600, end=now())
        assert daily_usage(board, now())["runtime_seconds"] == 20 * 3600
    finally:
        other.close()


def test_unreadable_registered_accounting_does_not_grant_new_run(board, tmp_path):
    path = tmp_path / "kanban" / "boards" / "broken"
    path.mkdir(parents=True)
    kb.write_board_metadata("broken", name="Broken accounting fixture")
    broken = sqlite3.connect(path / "kanban.db")
    broken.close()
    tid = card(board)
    with pytest.raises(sqlite3.OperationalError):
        kb.claim_task(board, tid)
    assert not kb.list_runs(board, tid)
    assert kb.get_task(board, tid).status == "ready"


def test_native_coordinator_card_gets_mara_one_extra_series(board):
    tid = kb.create_task(board, title="Native coordinator task", assignee="otto",
                         created_by="coordinator", body="No MCP ingress metadata.")
    for _ in range(5):
        attempt(board, tid)
    assert kb.claim_task(board, tid) is None
    decision = disposition(board, tid, "new_series", limit_event_id=hits(board, tid)[0][0].id)
    response = native_decision(board, tid, decision)
    assert response["ok"] is True
    assert list(supervisor_decisions(board, tid))[-1][1] == decision
    assert kb.get_task(board, tid).status == "blocked"  # Judgment is not a free unblock.
    assert kb.unblock_task(board, tid)
    for _ in range(5):
        attempt(board, tid)
    assert kb.claim_task(board, tid) is None
    again = disposition(board, tid, "new_series", limit_event_id=hits(board, tid)[-1][0].id)
    assert "binding_invalid" in native_decision(board, tid, again)["error"]
    assert len(list(supervisor_decisions(board, tid))) == 1


@pytest.mark.parametrize("work_class,assignee", [("incident_work", "otto"), ("noor_day_work", "noor")])
def test_native_coordinator_classification_allows_only_bound_daily_exception(board, tmp_path, work_class, assignee):
    tid = kb.create_task(board, title="Native task", assignee=assignee,
                         created_by="coordinator", body="Original task instructions.")
    decision = disposition(board, tid, "classification", work_class=work_class)
    assert native_decision(board, tid, decision)["ok"] is True
    other = other_board(tmp_path)
    try:
        attempt(other, card(other), start=now() - 20 * 3600, end=now())
        with patch("hermes_cli.kanban_db.time.time", return_value=now()):
            assert kb.claim_task(board, tid) is not None
        payload = hits(board, tid)[0][1]
        assert payload["action"] == "exception_allowed" and payload["exception_class"] == work_class
    finally:
        other.close()


def test_non_mara_runtime_cannot_grant_by_body_or_actor_string(board):
    tid = card(board)
    decision = disposition(board, tid, "classification", work_class="incident_work")
    before = len(kb.list_comments(board, tid))
    assert "supervisor_required" in native_decision(board, tid, decision, identity="otto")["error"]
    assert len(kb.list_comments(board, tid)) == before
    assert not list(supervisor_decisions(board, tid))
    # A direct comment without the identity-checked producer event grants nothing.
    kb.add_comment(board, tid, author="mara", body=json.dumps(decision))
    assert not list(supervisor_decisions(board, tid))


def test_native_decision_rejects_cross_task_and_stale_execution_prefix_atomically(board):
    tid = card(board)
    other = card(board)
    decision = disposition(board, tid, "classification", work_class="incident_work")
    assert "binding_invalid" in native_decision(board, other, decision)["error"]
    attempt(board, tid)
    assert "binding_invalid" in native_decision(board, tid, decision)["error"]
    assert not kb.list_comments(board, tid) and not kb.list_comments(board, other)


def test_native_measured_closure_resets_only_the_measured_series(board):
    tid = card(board)
    for _ in range(5):
        attempt(board, tid)
    assert kb.claim_task(board, tid) is None
    choice = disposition(board, tid, "measured_closure", case_ref="oos://cases/fixture",
                         measurement_ref="oos://measurements/fixture")
    assert native_decision(board, tid, choice)["ok"] is True
    assert kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid) is not None


def test_native_decision_event_and_comment_rollback_together(board):
    tid = card(board)
    choice = disposition(board, tid, "classification", work_class="incident_work")
    original = kb._append_event
    def fail_decision(conn, task_id, kind, *args, **kwargs):
        if kind == "run_limit_decision":
            raise RuntimeError("fixture failed decision receipt")
        return original(conn, task_id, kind, *args, **kwargs)
    with patch.object(kb, "_append_event", side_effect=fail_decision):
        assert "fixture failed" in native_decision(board, tid, choice)["error"]
    assert not kb.list_comments(board, tid)
    assert not list(supervisor_decisions(board, tid))
