"""SQLite regressions for composing review reopen inside an outer write."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _review_task(conn, *, title: str = "review me", assignee: str = "worker") -> str:
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    claimed = kb.claim_task(conn, task_id, claimer="worker:test")
    assert claimed is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready for review",
        reviewer="reviewer:test",
        expected_run_id=claimed.current_run_id,
    )
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.assignee == "reviewer:test"
    return task_id


def _events(conn, task_id: str, kind: str | None = None):
    events = kb.list_events(conn, task_id)
    return [event for event in events if kind is None or event.kind == kind]


def _row(conn, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def test_reopen_review_task_rejects_nested_transaction_by_default(conn) -> None:
    task_id = _review_task(conn)

    with pytest.raises(RuntimeError, match="Nested composition must opt in"):
        with kb.write_txn(conn):
            kb.reopen_review_task(conn, task_id)

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.assignee == "reviewer:test"
    assert not _events(conn, task_id, "review_reopened")


def test_nested_reopen_review_rolls_back_with_outer_receipt_and_run_reclaim(conn) -> None:
    task_id = _review_task(conn)
    with kb.write_txn(conn):
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at)
            VALUES (?, 'worker', 'running', 123)
            """,
            (task_id,),
        )
        stale_run_id = int(run_cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (stale_run_id, task_id),
        )

    with pytest.raises(RuntimeError, match="rollback outer review receipt"):
        with kb.write_txn(conn):
            kb.add_comment(conn, task_id, author="adapter", body="receipt: rework-1")
            assert kb.reopen_review_task(conn, task_id, allow_nested=True) is True
            reopened = kb.get_task(conn, task_id)
            assert reopened is not None
            assert reopened.status == "ready"
            assert reopened.current_run_id is None
            assert _events(conn, task_id, "review_reopened")
            raise RuntimeError("rollback outer review receipt")

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.assignee == "reviewer:test"
    assert task.current_run_id == stale_run_id
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
        (stale_run_id,),
    ).fetchone()
    assert run is not None
    assert run["status"] == "running"
    assert run["outcome"] is None
    assert run["ended_at"] is None
    assert [c.body for c in kb.list_comments(conn, task_id)] == []
    assert not _events(conn, task_id, "review_reopened")


def test_nested_reopen_review_commit_preserves_parent_gate_implementer_and_recurrences(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="builder")
    task_id = kb.create_task(conn, title="review child", assignee="builder", parents=[parent])
    assert kb.complete_task(conn, parent, result="initial parent result")
    implementation = kb.claim_task(conn, task_id, claimer="builder:test")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready for review",
        reviewer="reviewer:test",
        expected_run_id=implementation.current_run_id,
    )
    reviewing = kb.get_task(conn, task_id)
    assert reviewing is not None
    assert reviewing.status == "review"
    assert reviewing.assignee == "reviewer:test"
    # A previous blocker before review is intentionally preserved across review
    # reopen; only successful completion clears this counter.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET block_recurrences = 2, block_kind = 'capability' WHERE id = ?",
            (task_id,),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))

    with kb.write_txn(conn):
        kb.add_comment(conn, task_id, author="adapter", body="receipt: rework-2")
        assert kb.reopen_review_task(conn, task_id, allow_nested=True) is True

    parent_gated = kb.get_task(conn, task_id)
    assert parent_gated is not None
    assert parent_gated.status == "todo"
    assert parent_gated.assignee == "builder"
    assert parent_gated.current_run_id is None
    assert parent_gated.block_recurrences == 2
    assert parent_gated.block_kind == "capability"
    reopened_payload = _events(conn, task_id, "review_reopened")[-1].payload
    assert reopened_payload == {"status": "todo", "implementer": "builder"}
    review_payload = _events(conn, task_id, "review_requested")[-1].payload
    assert review_payload["implementer"] == "builder"
    assert review_payload["reviewer"] == "reviewer:test"
    assert [c.body for c in kb.list_comments(conn, task_id)] == ["receipt: rework-2"]

    assert kb.complete_task(conn, parent, result="parent redone")
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb.get_task(conn, task_id).block_recurrences == 2
