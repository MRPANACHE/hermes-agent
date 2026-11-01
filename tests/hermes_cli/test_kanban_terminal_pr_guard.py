"""Real SQLite + dispatcher regression for terminal parent PRs and bounded guards."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli import kanban_pr_acceptance as acceptance

PARENT_PR = "https://github.com/example/repo/pull/3164"
OWN_PR = "https://github.com/example/repo/pull/3165"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(acceptance, "_assignee_profile_home", lambda name: str(tmp_path / name))
    monkeypatch.setattr(acceptance, "_gh_env", lambda home: {"GH_CONFIG_DIR": str(tmp_path / "isolated-gh")})
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def resolved_parent(conn):
    parent = kb.create_task(conn, title="delivery", assignee="owner")
    child = kb.create_task(conn, title="existing followthrough", assignee="builder")
    old = kb.claim_task(conn, child)
    assert old is not None
    kb.link_tasks(conn, parent, child, expected_child_run_id=old.current_run_id)
    assert kb.block_task(conn, child, reason="wait for delivery", kind="dependency", expected_run_id=old.current_run_id)
    parent_run = kb.claim_task(conn, parent)
    assert parent_run is not None
    kb.add_comment(conn, child, author="owner", body=f"Delivery {PARENT_PR}")
    assert kb.complete_task(conn, parent, summary="delivered", metadata={"pr_url": PARENT_PR}, expected_run_id=parent_run.current_run_id)
    kb.recompute_ready(conn)
    kb.add_comment(conn, child, author="coordinator", body="ordinary triage readback")
    kb.add_comment(conn, child, author="builder", body="ordinary progress; retain original task")
    return child, parent, old.current_run_id


def tick(conn, tid):
    result = dispatch.DispatchResult()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    dispatch._dispatch_lane_task(conn, row, row["assignee"], result,
        lane="ready", dry_run=False, ttl_seconds=900, board=None,
        failure_limit=2, spawn_fn=lambda task, workspace: None,
        per_profile_cap=None, per_profile_running={})
    return result


def gh_reply(state="closed", merged=True, *, url=PARENT_PR):
    return subprocess.CompletedProcess([], 0, json.dumps({"html_url": url, "state": state, "merged": merged}), "")


def test_prior_run_parent_pr_and_later_comments_spawn_next_tick(board):
    tid, _parent, old = resolved_parent(board)
    with patch("subprocess.run", return_value=gh_reply(merged=True)):
        result = tick(board, tid)
    assert [s[0] for s in result.spawned] == [tid]
    task = kb.get_task(board, tid)
    assert task is not None
    assert task.status == "running" and task.current_run_id != old
    assert kb.claim_task(board, tid) is None
    assert board.execute("SELECT outcome FROM task_runs WHERE id=?", (old,)).fetchone()[0] == "blocked"
    assert len(kb.list_comments(board, tid)) == 3


@pytest.mark.parametrize("reply", ["open", "closed-unmerged", "timeout", "network", "bad-json", "wrong-pr", "partial"])
def test_open_unknown_or_network_does_not_stop_owner(board, reply):
    tid, _, _ = resolved_parent(board)
    # PR access is irrelevant to claiming an authorized existing assignment.
    with patch("subprocess.run", side_effect=AssertionError(reply)) as lookup:
        assert [s[0] for s in tick(board, tid).spawned] == [tid]
        lookup.assert_not_called()
    assert kb.get_task(board, tid).status == "running"
    assert len(kb.list_runs(board, tid)) == 2
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,)).fetchone()


def test_independent_open_own_pr_is_context_for_same_owner(board):
    tid, _, _ = resolved_parent(board)
    kb.add_comment(board, tid, author="builder", body=f"My independent PR {OWN_PR}")
    with patch("subprocess.run", side_effect=AssertionError("No PR authority lookup")):
        assert [s[0] for s in tick(board, tid).spawned] == [tid]
    assert kb.get_task(board, tid).assignee == "builder"
    assert OWN_PR in kb.list_comments(board, tid)[-1].body


def test_historical_escalation_and_routine_comments_do_not_stop_owner(board):
    tid, _, _ = resolved_parent(board)
    # Preserve the historical episode as evidence, not a persistent policy.
    with kb.write_txn(board):
        kb._append_event(board, tid, "respawn_guarded", {"reason": "active_pr", "escalated": True})
    for _ in range(3):
        kb.add_comment(board, tid, author="coordinator", body="ordinary readback")
    with kb.connect() as restarted, patch("subprocess.run", side_effect=OSError("unreachable")):
        assert [s[0] for s in tick(restarted, tid).spawned] == [tid]
    events = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,)).fetchall()
    assert len(events) == 1 and json.loads(events[0][0])["escalated"]
    assert kb.claim_task(board, tid) is None


@pytest.mark.parametrize("mutation", ["comment", "owner", "parent", "expired"])
def test_current_claim_state_not_terminal_pr_receipt_controls_admission(board, mutation):
    tid, parent, _ = resolved_parent(board)
    if mutation == "comment":
        kb.add_comment(board, tid, author="builder", body=OWN_PR)
    elif mutation == "owner":
        kb.assign_task(board, tid, "other")
    elif mutation == "parent":
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,)); board.commit()
    else:
        board.execute("UPDATE task_comments SET created_at=created_at-86401 WHERE task_id=?", (tid,)); board.commit()
    task = kb.claim_task(board, tid)
    if mutation == "parent":
        assert task is None
        assert len(kb.list_runs(board, tid)) == 1
    else:
        assert task is not None
        assert task.assignee == ("other" if mutation == "owner" else "builder")
        assert kb.claim_task(board, tid) is None


@pytest.mark.parametrize("state", ["open", "unknown"])
def test_reference_age_is_not_an_execution_stop(board, state):
    tid, _, _ = resolved_parent(board)
    board.execute("UPDATE task_comments SET created_at=created_at-86401 WHERE task_id=?", (tid,))
    board.commit()
    with patch("subprocess.run", side_effect=AssertionError(state)):
        assert len(tick(board, tid).spawned) == 1


def test_no_profile_auth_does_not_borrow_dispatcher_credentials(board, monkeypatch):
    tid, _, _ = resolved_parent(board)
    monkeypatch.setattr(acceptance, "_gh_env", lambda home: (_ for _ in ()).throw(acceptance._GateAuthError("unavailable")))
    with patch("subprocess.run", side_effect=AssertionError("No borrowed credentials")) as run:
        assert len(tick(board, tid).spawned) == 1
        run.assert_not_called()


def test_many_pr_references_need_no_lookup_budget(board):
    tid, _, _ = resolved_parent(board)
    for n in range(10, 17):
        kb.add_comment(board, tid, author="builder", body=f"https://github.com/example/repo/pull/{n}")
    with patch("subprocess.run", side_effect=AssertionError("No PR lookup")) as run:
        assert len(tick(board, tid).spawned) == 1
        run.assert_not_called()


def test_full_dispatch_tick_claims_once_without_terminal_pr_lookup(board):
    tid, _, _ = resolved_parent(board)
    with patch("subprocess.run", return_value=gh_reply()), patch.object(dispatch, "_memory_pressure_level", return_value="ok"):
        first = kb.dispatch_once(board, max_in_progress=1, reconcile_orphans=False, spawn_fn=lambda task, workspace: None)
        second = kb.dispatch_once(board, max_in_progress=1, reconcile_orphans=False, spawn_fn=lambda task, workspace: None)
    assert [s[0] for s in first.spawned] == [tid]
    assert not second.spawned
