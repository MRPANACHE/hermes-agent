"""Real native completion → SQLite → same-run continuation, no live providers."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import goals, kanban_db as kb
from tools import kanban_tools as kt
from tools.registry import registry


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "isolated.sqlite"))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        body = json.dumps({"schema": "mrpanache.agent-request.v1", "agent": "otto",
                           "kind": "codework", "delivery_goal": "draft_pr"}) + "\nDeliver tested draft only. No merge or deployment."
        tid = kb.create_task(conn, title="Complete the current scoped objective", body=body,
                             assignee="test-worker", goal_mode=True, goal_max_turns=3,
                             workspace_kind="dir", workspace_path=str(tmp_path))
        task = kb.claim_task(conn, tid)
        assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    calls = []

    def judge(**kwargs):
        calls.append(kwargs)
        return "done", "Partial deliverable accepted", False, None, False

    monkeypatch.setattr(kt, "judge_goal", judge)
    return tid, task.current_run_id, calls


def read(tid):
    with kb.connect() as conn:
        task, run = kb.get_task(conn, tid), kb.latest_run(conn, tid)
        assert task is not None
        return task, run


def complete(metadata):
    return json.loads(registry.dispatch("kanban_complete", {
        "summary": "Tested draft slice accepted", "metadata": metadata}))


@pytest.mark.parametrize("field", ["objective_complete", "goal_complete"])
@pytest.mark.parametrize("judge_state", ["done", "unavailable", "transport_failure"])
def test_explicit_unmet_current_goal_survives_positive_partial_acceptance(worker, monkeypatch, field, judge_state):
    tid, run_id, calls = worker
    if judge_state == "unavailable":
        monkeypatch.setattr(kt, "_goal_judge_available", lambda: False)
    elif judge_state == "transport_failure":
        def fail(**kwargs):
            raise RuntimeError("isolated provider unavailable")
        monkeypatch.setattr(kt, "judge_goal", fail)
    out = complete({field: False, "acceptance": {"complete": True}, "evidence_refs": ["fixture:partial-draft"]})
    task, run = read(tid)
    assert out.get("error"), out
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert run.ended_at is None
    assert run.outcome is None
    if judge_state == "done":
        assert len(calls) == 1  # canonical boundary already judged, not outer-loop proof
        assert calls[0]["exact_goal"] is True
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='completed'").fetchone()[0] == 0


def test_same_native_loop_executes_remaining_work_and_finishes_same_run(worker, monkeypatch, tmp_path):
    tid, run_id, calls = worker
    initial = complete({"goal_complete": False, "acceptance": {"complete": True}})
    assert initial.get("error")
    prompts = []
    result_path = tmp_path / "executed-result.json"
    payload = tmp_path / "draft-payload.bin"
    payload.write_bytes(b"isolated scoped draft payload\n")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "current objective unmet", False, None, False))

    def turn(prompt):
        prompts.append(prompt)
        # Execute a real bounded action, not an ACK or a claimed receipt.
        # Only the provider/turn decision is injected.
        execution = subprocess.run([sys.executable, "-c",
            "import hashlib,json,sys; from pathlib import Path; "
            "print(json.dumps({'sha256':hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest()}))",
            str(payload)], check=True, capture_output=True, text=True, timeout=10)
        result_path.write_text(execution.stdout)
        receipt = json.loads(result_path.read_text())
        assert receipt["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
        assert complete({"goal_complete": True, "evidence_refs": [str(result_path)]}).get("ok")
        return "Executed remaining scoped work and completed."

    outcome = goals.run_kanban_goal_loop(task_id=tid, goal_text=read(tid)[0].body,
        first_response=json.dumps(initial), run_turn=turn,
        task_status_fn=lambda: read(tid)[0].status,
        block_fn=lambda reason: pytest.fail(reason), max_turns=3)
    assert outcome["outcome"] == "completed_by_worker"
    assert outcome["turns_used"] == 2
    assert len(prompts) == 1
    assert json.loads(result_path.read_text())["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
    task, run = read(tid)
    assert task.status == "done"
    assert run.id == run_id and run.outcome == "completed"
    assert run.metadata["goal_complete"] is True
    assert task.body.endswith("No merge or deployment.")
    assert len(calls) == 2  # one canonical judgment per attempted completion


@pytest.mark.parametrize("metadata", [{}, {"goal_complete": True}, {"objective_complete": True},
    {"parent_objective": {"goal_complete": False}}, {"goal_complete": None}, {"goal_complete": 0},
    {"goal_complete": "false"}])
def test_no_generic_loop_for_absent_nonboolean_or_parent_goal_fields(worker, metadata):
    tid, _, calls = worker
    assert complete(metadata).get("ok")
    assert read(tid)[0].status == "done"
    assert len(calls) == 1


def test_non_goal_task_keeps_partial_handoff_semantics(worker):
    tid, _, _ = worker
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET goal_mode=0 WHERE id=?", (tid,))
        conn.commit()
    assert complete({"goal_complete": False}).get("ok")
    assert read(tid)[0].status == "done"


def test_partial_review_stays_review_not_goal_completion(worker):
    tid, _, calls = worker
    out = json.loads(kt._handle_request_review({"summary": "Scoped implementation ready for review",
                                              "metadata": {"goal_complete": False}}))
    assert out.get("ok"), out
    assert read(tid)[0].status == "review"
    assert "implementation review readiness" in calls[0]["goal"]


def test_budget_stops_same_task_without_followup(worker, monkeypatch):
    tid, run_id, _ = worker
    initial = complete({"objective_complete": False, "acceptance": {"complete": True}})
    assert initial.get("error")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "unmet", False, None, False))
    reasons = []
    def block(reason):
        reasons.append(reason)
        with kb.connect() as conn:
            assert kb.block_task(conn, tid, reason=reason, expected_run_id=run_id)
    out = goals.run_kanban_goal_loop(task_id=tid, goal_text=read(tid)[0].body,
        first_response=json.dumps(initial), max_turns=1,
        run_turn=lambda prompt: pytest.fail("must not exceed budget"),
        task_status_fn=lambda: read(tid)[0].status, block_fn=block)
    assert out["outcome"] == "blocked_budget"
    assert read(tid)[0].status == "blocked"
    assert "turn budget" in reasons[0]
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("blocker", ["capability", "approval"])
def test_external_blocker_uses_existing_needs_input_route(worker, blocker):
    tid, _, _ = worker
    assert complete({"goal_complete": False}).get("error")
    reason = f"{blocker} unavailable: owning maintainer must provide existing scoped route; next check after owner answer"
    out = json.loads(kt._handle_block({"kind": "needs_input", "reason": reason}))
    assert out.get("ok"), out
    assert read(tid)[0].status == "blocked"
    assert read(tid)[1].summary == reason


def test_userstop_no_continuation(worker, monkeypatch):
    tid, run_id, _ = worker
    with kb.connect() as conn:
        assert kb.archive_task(conn, tid)
    out = goals.run_kanban_goal_loop(task_id=tid, goal_text="current goal",
        first_response="goal incomplete", task_status_fn=lambda: read(tid)[0].status,
        run_turn=lambda prompt: pytest.fail("userstop must not execute"),
        block_fn=lambda reason: pytest.fail(reason))
    assert out["outcome"] == "stopped"


def test_replay_and_concurrent_completions_never_create_duplicate_run_or_task(worker):
    tid, run_id, _ = worker
    def attempt(_):
        return complete({"goal_complete": True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(attempt, range(2)))
    assert sum(reply.get("ok") is True for reply in replies) == 1
    assert complete({"goal_complete": True}).get("error")
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='completed'").fetchone()[0] == 1
        assert kb.latest_run(conn, tid).id == run_id


def test_stale_worker_cannot_complete_new_run(worker, monkeypatch):
    tid, old_run, _ = worker
    with kb.connect() as conn:
        assert kb.reclaim_task(conn, tid, reason="fixture stale")
        new = kb.claim_task(conn, tid)
    assert new.current_run_id != old_run
    assert complete({"goal_complete": True}).get("error")
    assert read(tid)[0].current_run_id == new.current_run_id
    assert read(tid)[0].status == "running"


def test_existing_child_dependency_waits_until_current_goal_reached(worker):
    tid, _, _ = worker
    with kb.connect() as conn:
        child = kb.create_task(conn, title="precreated review", assignee="test-reviewer", parents=[tid])
    assert complete({"goal_complete": False}).get("error")
    assert read(child)[0].status == "todo"
    with kb.connect() as conn:
        assert kb.claim_task(conn, child) is None
    assert complete({"goal_complete": True}).get("ok")
    with kb.connect() as conn:
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, child) is not None


def test_cli_sibling_cannot_bypass_explicit_current_goal_fact(worker):
    from hermes_cli.kanban import _cmd_complete
    tid, _, _ = worker
    args = argparse.Namespace(task_ids=[tid], summary="partial draft accepted", result=None,
                              metadata=json.dumps({"objective_complete": False}))
    # DB is the shared persistence boundary, not a second auxiliary judge.
    assert _cmd_complete(args) != 0
    assert read(tid)[0].status == "running"


@pytest.mark.parametrize("bulk", [False, True])
def test_dashboard_returns_controlled_rejection_without_closing_run(worker, bulk):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    tid, run_id, _ = worker
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("goal_completion_dashboard_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/kanban")
    client = TestClient(app)
    payload = {"status": "done", "summary": "partial draft", "metadata": {"goal_complete": False}}
    if bulk:
        payload["ids"] = [tid]
        response = client.post("/kanban/tasks/bulk", json=payload)
        assert response.status_code == 200
        entry = response.json()["results"][0]
        assert entry["ok"] is False and "objective" in entry["error"]
    else:
        response = client.patch(f"/kanban/tasks/{tid}", json=payload)
        assert response.status_code == 409
        assert "objective" in response.json()["detail"]
    task, run = read(tid)
    assert task.status == "running" and task.current_run_id == run_id
    assert run.ended_at is None
