"""A timed wait is nonterminal: ordinary evidence-backed completion may resolve it."""

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def test_complete_scheduled_preserves_history_artifacts_and_never_wakes(conn, tmp_path):
    task = kb.create_task(conn, title="incident", assignee="otto")
    kb.claim_task(conn, task)
    artifact = tmp_path / "proof.txt"
    artifact.write_text("original measured evidence")
    kb.store_attachment_bytes(conn, task, artifact.name, artifact.read_bytes(), uploaded_by="otto")
    assert kb.request_review(conn, task, summary="Original measured handoff",
                             metadata={"evidence": "original", "artifacts": [str(artifact)]})
    assert kb.schedule_task(conn, task, wake_at="2000-01-01T00:00:00Z", reason="ordinary recheck")
    runs_before = [dict(row) for row in conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task,))]
    schedule_before = [dict(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (task,))]
    attachments_before = [dict(row) for row in conn.execute(
        "SELECT * FROM task_attachments WHERE task_id=?", (task,))]
    assert attachments_before

    assert kb.complete_task(conn, task, result="Two clean source assessments",
                            metadata={"evidence": "independent-readback"})
    assert kb.get_task(conn, task).status == "done"
    assert kb.get_task(conn, task).current_run_id is None
    assert [dict(row) for row in conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task,))][:len(runs_before)] == runs_before
    assert [dict(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (task,))] == schedule_before
    assert [dict(row) for row in conn.execute(
        "SELECT * FROM task_attachments WHERE task_id=?", (task,))] == attachments_before
    assert artifact.read_text() == "original measured evidence"
    assert kb.dispatch_once(conn, max_spawn=0).awakened == []
    assert kb.dispatch_once(conn, max_spawn=0).awakened == []
    assert kb.get_task(conn, task).status == "done"
    assert not any(event.kind == "schedule_woke" for event in kb.list_events(conn, task))
    context = kb.build_worker_context(conn, task)
    assert "Original measured handoff" in context
    assert "independent-readback" in context


def test_scheduled_completion_keeps_parent_gate(conn):
    parent = kb.create_task(conn, title="open prerequisite")
    task = kb.create_task(conn, title="incident", parents=[parent])
    assert kb.schedule_task(conn, task, reason="wait")
    assert not kb.complete_task(conn, task, summary="Source now clean")
    assert kb.get_task(conn, task).status == "scheduled"
    assert kb.complete_task(conn, parent, summary="Prerequisite actually delivered")
    assert kb.complete_task(conn, task, summary="Source now clean")


def test_scheduled_completion_keeps_evidence_and_run_ownership_gates(conn):
    task = kb.create_task(conn, title="incident", assignee="otto")
    kb.claim_task(conn, task)
    run_id = kb.get_task(conn, task).current_run_id
    assert kb.schedule_task(conn, task, reason="wait", expected_run_id=run_id)
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, task, summary="   ")
    assert not kb.complete_task(conn, task, summary="stale worker", expected_run_id=run_id)
    assert kb.get_task(conn, task).status == "scheduled"
    assert kb.complete_task(conn, task, summary="Independent clean assessments")


def test_completion_cannot_take_over_live_worker(conn, monkeypatch):
    task = kb.create_task(conn, title="live incident", assignee="otto")
    kb.claim_task(conn, task)
    run_id = kb.get_task(conn, task).current_run_id
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), task))
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at: True)
    with pytest.raises(kb.LiveClaimError):
        kb.complete_task(conn, task, summary="external assessment")
    assert not kb.complete_task(conn, task, summary="stale worker", expected_run_id=run_id + 1)
    assert kb.get_task(conn, task).status == "running"
    assert kb.get_task(conn, task).current_run_id == run_id


def test_scheduled_completion_keeps_explicit_incomplete_goal_gate(conn):
    task = kb.create_task(conn, title="goal incident", goal_mode=True)
    assert kb.schedule_task(conn, task, reason="wait")
    with pytest.raises(ValueError, match="objective explicitly incomplete"):
        kb.complete_task(conn, task, summary="Partial assessment", metadata={"objective_complete": False})
    assert kb.get_task(conn, task).status == "scheduled"
