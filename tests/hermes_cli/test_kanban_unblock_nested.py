"""SQLite regressions for composing kanban unblocks inside an outer write."""

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


def _block_ready_task(conn, *, title: str = "blocked") -> str:
    task_id = kb.create_task(conn, title=title, assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer="worker:test")
    assert claimed is not None
    assert kb.block_task(
        conn,
        task_id,
        reason="needs operator input",
        kind="needs_input",
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "blocked"
    return task_id


def _events(conn, task_id: str, kind: str | None = None):
    events = kb.list_events(conn, task_id)
    return [event for event in events if kind is None or event.kind == kind]


def test_unblock_task_rejects_nested_transaction_by_default(conn) -> None:
    task_id = _block_ready_task(conn)

    with pytest.raises(RuntimeError, match="Nested composition must opt in"):
        with kb.write_txn(conn):
            kb.unblock_task(conn, task_id)

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert not _events(conn, task_id, "unblocked")


def test_nested_unblock_rolls_back_with_outer_receipt_and_run_reclaim(conn) -> None:
    task_id = _block_ready_task(conn)
    stale = kb.claim_task(conn, task_id, claimer="stale-worker")
    assert stale is None
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

    with pytest.raises(RuntimeError, match="rollback outer receipt"):
        with kb.write_txn(conn):
            kb.add_comment(conn, task_id, author="adapter", body="receipt: req-1")
            assert kb.unblock_task(conn, task_id, allow_nested=True) is True
            assert kb.get_task(conn, task_id).status == "ready"
            assert _events(conn, task_id, "unblocked")
            raise RuntimeError("rollback outer receipt")

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
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
    assert not _events(conn, task_id, "unblocked")


def test_nested_unblock_commit_preserves_review_resume_parent_gate_and_recurrences(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="builder")
    task_id = kb.create_task(
        conn,
        title="review child",
        assignee="builder",
        parents=[parent],
    )
    assert kb.complete_task(conn, parent, result="initial parent result")
    implementation = kb.claim_task(conn, task_id, claimer="builder:test")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready for review",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:test")
    assert review is not None
    assert kb.block_task(
        conn,
        task_id,
        reason="review environment unavailable",
        kind="capability",
        expected_run_id=review.current_run_id,
    )
    blocked = kb.get_task(conn, task_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.block_recurrences == 1

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))

    with kb.write_txn(conn):
        kb.add_comment(conn, task_id, author="adapter", body="receipt: req-2")
        assert kb.unblock_task(conn, task_id, allow_nested=True) is True

    parent_gated = kb.get_task(conn, task_id)
    assert parent_gated is not None
    assert parent_gated.status == "todo"
    assert parent_gated.current_run_id is None
    assert parent_gated.block_recurrences == 1
    unblocked_payload = _events(conn, task_id, "unblocked")[-1].payload
    assert unblocked_payload == {"status": "todo", "resume_status": "review"}
    assert [c.body for c in kb.list_comments(conn, task_id)] == ["receipt: req-2"]

    assert kb.complete_task(conn, parent, result="parent redone")
    assert kb.get_task(conn, task_id).status == "review"
    assert kb.get_task(conn, task_id).block_recurrences == 1
