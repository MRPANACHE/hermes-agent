"""Supervisor continuation preserves native work and enforces existing limits."""
import json
import os
import subprocess
import sys

import pytest

from hermes_cli import goals, kanban_db as kb


@pytest.fixture
def exhausted(tmp_path, monkeypatch):
    from hermes_cli import profiles
    profile = profiles._get_profiles_root() / "otto"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    workspace = tmp_path / "retained-work"
    workspace.mkdir()
    (workspace / "work.txt").write_text("existing change: 41")
    conn = kb.connect()
    task_id = kb.create_task(conn, title="Finish the existing file using the prior decision",
                             assignee="otto", workspace_kind="dir", workspace_path=str(workspace),
                             goal_mode=True, goal_max_turns=1, max_runtime_seconds=300,
                             initial_status="blocked")
    assert kb.unblock_task(conn, task_id)
    kb.add_comment(conn, task_id, "david", "Decision: add one; preserve existing file.")
    task = kb.claim_task(conn, task_id)
    monkeypatch.setattr(goals, "judge_goal", lambda *args: ("continue", "remaining work", False, None, False))
    result = goals.run_kanban_goal_loop(
        task_id=task_id, goal_text=task.title, first_response="File retained; decision add one read.",
        max_turns=1, run_turn=lambda _prompt: pytest.fail("must block before spending another turn"),
        task_status_fn=lambda: kb.goal_run_status(conn, task_id, task.current_run_id),
        block_fn=lambda _reason: pytest.fail("budget needs a typed checkpoint"),
        budget_block_fn=lambda reason, **checkpoint: kb.block_goal_budget(
            conn, task_id, expected_run_id=task.current_run_id, reason=reason,
            usage={"session_input_tokens": 17}, session_id="fixture-session", **checkpoint))
    assert result["outcome"] == "blocked_budget"
    assert kb.get_task(conn, task_id).block_kind == "turn_budget"
    yield conn, task_id, workspace
    conn.close()


def test_real_dispatch_continuation_uses_file_and_decision_once(exhausted):
    conn, task_id, workspace = exhausted
    binding = kb.goal_budget_binding(conn, task_id)
    assert binding["history"]["turns_used"] == 1
    assert binding["history"]["usage"]["session_input_tokens"] == 17
    assert kb.unblock_task(conn, task_id)
    # Different supervisor request identities cannot consume this block twice.
    assert not kb.unblock_task(conn, task_id)
    processes = []
    script = """
import json, os
from pathlib import Path
from hermes_cli import kanban_db as kb
conn = kb.connect()
task = kb.get_task(conn, os.environ['FIXTURE_TASK'])
context = kb.build_worker_context(conn, task.id)
assert 'Decision: add one' in context
assert 'File retained; decision add one read.' in context
file = Path('work.txt')
assert file.read_text() == 'existing change: 41'
file.write_text('controlled result: 42')
kb.complete_task(conn, task.id, result='Used existing file and prior decision: 42',
                 expected_run_id=task.current_run_id)
conn.close()
"""
    def spawn(task, path, **kwargs):
        assert task.id == task_id and path == str(workspace)
        process = subprocess.Popen([sys.executable, "-c", script], cwd=path,
                                   env={**os.environ, "FIXTURE_TASK": task.id,
                                        "PYTHONPATH": os.getcwd()})
        processes.append(process)
        return process.pid
    kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    for process in processes:
        assert process.wait(timeout=10) == 0
    kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    assert len(processes) == 1
    assert len(kb.list_runs(conn, task_id)) == 2
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, task_id).workspace_path == str(workspace)
    assert (workspace / "work.txt").read_text() == "controlled result: 42"


@pytest.mark.parametrize("change", ["scheduled", "done", "archived", "assignment"])
def test_later_control_prevents_claim(exhausted, change):
    conn, task_id, _workspace = exhausted
    assert kb.unblock_task(conn, task_id)
    if change == "assignment":
        conn.execute("UPDATE tasks SET body='changed assignment' WHERE id=?", (task_id,))
    else:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (change, task_id))
    assert kb.claim_task(conn, task_id) is None
    assert len(kb.list_runs(conn, task_id)) == 1


@pytest.mark.parametrize("limit", ["runtime", "recurrences", "retries"])
def test_exhausted_limits_require_explicit_action(exhausted, limit):
    conn, task_id, _workspace = exhausted
    if limit == "runtime":
        conn.execute("UPDATE task_runs SET started_at=ended_at-300 WHERE task_id=?", (task_id,))
    elif limit == "recurrences":
        conn.execute("UPDATE tasks SET block_recurrences=? WHERE id=?", (kb.BLOCK_RECURRENCE_LIMIT, task_id))
    else:
        conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (kb.DEFAULT_FAILURE_LIMIT, task_id))
    with pytest.raises(ValueError, match="explicit operator action required"):
        kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == "blocked"


def test_retry_use_is_not_reset(exhausted):
    conn, task_id, _workspace = exhausted
    conn.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (task_id,))
    assert kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).consecutive_failures == 1


def test_new_budget_block_carries_cumulative_usage(exhausted):
    conn, task_id, _workspace = exhausted
    assert kb.unblock_task(conn, task_id)
    task = kb.claim_task(conn, task_id)
    assert kb.block_goal_budget(conn, task_id, expected_run_id=task.current_run_id,
                               reason="second slice exhausted", turns_used=1,
                               last_response="remaining work", usage={"session_input_tokens": 19})
    history = kb.goal_budget_history(conn, task_id)
    assert history["turns_used"] == 2
    assert history["usage"]["session_input_tokens"] == 36
    assert history["chain_start_run"] == kb.list_runs(conn, task_id)[0].id
    with pytest.raises(ValueError, match="continuation_limit_reached"):
        kb.unblock_task(conn, task_id)


def test_changed_assignment_before_decision_denies(exhausted):
    conn, task_id, _workspace = exhausted
    conn.execute("UPDATE tasks SET body='changed assignment' WHERE id=?", (task_id,))
    with pytest.raises(ValueError, match="assignment_changed"):
        kb.unblock_task(conn, task_id)


def test_live_budget_worker_is_not_duplicated(exhausted):
    conn, task_id, _workspace = exhausted
    run = kb.list_runs(conn, task_id)[-1]
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "spawned", {"pid": os.getpid()}, run_id=run.id)
    with pytest.raises(ValueError, match="worker_exit_not_confirmed"):
        kb.unblock_task(conn, task_id)


def test_review_and_rework_have_their_existing_attempt_runtime(exhausted):
    conn, task_id, _workspace = exhausted
    conn.execute("UPDATE task_runs SET started_at=ended_at-260 WHERE task_id=?", (task_id,))
    assert kb.unblock_task(conn, task_id)
    task = kb.claim_task(conn, task_id)
    assert kb.request_review(conn, task_id, summary="implementation ready", expected_run_id=task.current_run_id)
    conn.execute("UPDATE task_runs SET started_at=ended_at-30 WHERE id=?", (task.current_run_id,))
    assert kb.reopen_review_task(conn, task_id)
    task = kb.claim_task(conn, task_id)
    assert task is not None
    assert not kb.goal_budget_history(conn, task_id)["active_chain"]
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(kb.time.time())-20, task.current_run_id))
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), task_id))
    signals = []
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: signals.append(args)) == []
    assert signals == []


def test_historical_block_needs_linked_execution_proof(exhausted):
    conn, task_id, _workspace = exhausted
    conn.execute("UPDATE tasks SET block_kind=NULL WHERE id=?", (task_id,))
    conn.execute("UPDATE task_runs SET metadata=NULL WHERE task_id=?", (task_id,))
    event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked'", (task_id,)).fetchone()
    payload = json.loads(event["payload"])
    payload["kind"] = None
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    assert kb.goal_budget_binding(conn, task_id)["expected_run_id"] == kb.list_runs(conn, task_id)[-1].id
    conn.execute("UPDATE task_runs SET summary='Human stop; needs permission' WHERE task_id=?", (task_id,))
    with pytest.raises(ValueError, match="execution_proof_missing"):
        kb.goal_budget_binding(conn, task_id)


def test_failure_to_finalize_is_not_turn_exhaustion(monkeypatch):
    monkeypatch.setattr(goals, "judge_goal", lambda *args: ("done", "looks complete", False, None, False))
    blocks = []
    result = goals.run_kanban_goal_loop(
        task_id="fixture", goal_text="finalize", first_response="done", max_turns=3,
        run_turn=lambda _prompt: "done", task_status_fn=lambda: "running",
        block_fn=blocks.append, budget_block_fn=lambda *_args, **_kwargs: pytest.fail("not a turn exhaustion"))
    assert result["outcome"] == "blocked_by_worker"
    assert len(blocks) == 1 and "never called kanban_complete" in blocks[0]
