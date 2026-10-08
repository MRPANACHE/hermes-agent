"""A failed auxiliary judge is infrastructure failure, not a task verdict."""
from hermes_cli import goals
import pytest


def test_transport_failure_does_not_spend_another_goal_turn(monkeypatch):
    monkeypatch.setattr(
        goals, "judge_goal",
        lambda *a, **kw: ("continue", "judge error: InternalServerError", False, None, True),
    )
    turns, blocks = [], []
    result = goals.run_kanban_goal_loop(
        task_id="original", goal_text="original unchanged acceptance",
        first_response="retained useful work", max_turns=3,
        task_status_fn=lambda: "running",
        run_turn=lambda prompt: turns.append(prompt) or "retained useful work",
        block_fn=blocks.append,
    )
    assert turns == []
    assert result["outcome"] == "blocked_judge_unavailable"
    assert result["turns_used"] == 1
    assert len(blocks) == 1
    assert "InternalServerError" in blocks[0]


def test_content_continue_still_spends_the_original_budget(monkeypatch):
    monkeypatch.setattr(
        goals, "judge_goal",
        lambda *a, **kw: ("continue", "acceptance is missing", False, None, False),
    )
    turns, blocks = [], []
    result = goals.run_kanban_goal_loop(
        task_id="original", goal_text="original unchanged acceptance",
        first_response="unfinished", max_turns=3,
        task_status_fn=lambda: "running",
        run_turn=lambda prompt: turns.append(prompt) or "unfinished",
        block_fn=blocks.append,
    )
    assert len(turns) == 2
    assert result["outcome"] == "blocked_budget"
    assert result["turns_used"] == 3


@pytest.mark.parametrize("status,kind", [(400, "capability"), (401, "capability"),
                                          (403, "capability"), (429, "transient"),
                                          (503, "transient")])
def test_http_error_kind(status, kind):
    assert goals.judge_transport_block_kind(f"judge error: APIError (HTTP {status})") == kind


def test_provider_reason_retains_only_safe_machine_fields(monkeypatch):
    from agent import auxiliary_client

    class InternalServerError(Exception):
        status_code = 503
        body = {"error": {"code": "model_included_exhausted", "message": "DO NOT COPY PROVIDER TEXT"}}

    def fail(**kwargs):
        raise InternalServerError("DO NOT COPY EXCEPTION TEXT")

    monkeypatch.setattr(auxiliary_client, "call_llm", fail)
    verdict, reason, parse_failed, wait, transport_failed = goals.judge_goal("original goal", "retained work")
    assert reason == "judge error: InternalServerError (HTTP 503, code=model_included_exhausted)"
    assert verdict == "continue" and transport_failed and not parse_failed and wait is None
    assert "DO NOT COPY" not in reason


def test_failed_block_write_does_not_report_block_success(monkeypatch):
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: (
        "continue", "judge error: InternalServerError", False, None, True))

    def fail(reason):
        raise RuntimeError("stale run")

    result = goals.run_kanban_goal_loop(
        task_id="original", goal_text="keep task", run_turn=lambda _: pytest.fail("unexpected extra work"),
        task_status_fn=lambda: "running", block_fn=fail, first_response="retained work", max_turns=3,
    )
    assert result["outcome"] == "stopped"
    assert result["turns_used"] == 1
