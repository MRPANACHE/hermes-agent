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


@pytest.mark.parametrize("merged", [True, False])
def test_prior_run_parent_pr_and_later_comments_spawn_next_tick(board, merged):
    tid, _parent, old = resolved_parent(board)
    with patch("subprocess.run", return_value=gh_reply(merged=merged)):
        result = tick(board, tid)
    assert [s[0] for s in result.spawned] == [tid]
    task = kb.get_task(board, tid)
    assert task is not None
    assert task.status == "running" and task.current_run_id != old
    assert kb.claim_task(board, tid) is None
    assert board.execute("SELECT outcome FROM task_runs WHERE id=?", (old,)).fetchone()[0] == "blocked"
    assert len(kb.list_comments(board, tid)) == 3


@pytest.mark.parametrize("reply", ["open", "timeout", "network", "bad-json", "wrong-pr", "partial"])
def test_open_unknown_or_network_never_spawns(board, reply):
    tid, _, _ = resolved_parent(board)
    kwargs = {
        "open": {"return_value": gh_reply("open", False)},
        "timeout": {"side_effect": subprocess.TimeoutExpired("gh", 3)},
        "network": {"side_effect": subprocess.CalledProcessError(1, "gh", stderr="secret")},
        "bad-json": {"return_value": subprocess.CompletedProcess([], 0, "invalid", "secret")},
        "wrong-pr": {"return_value": gh_reply(url=OWN_PR)},
        "partial": {"return_value": subprocess.CompletedProcess([], 0, '{"state":"closed"}', "")},
    }[reply]
    with patch("subprocess.run", **kwargs):
        assert not tick(board, tid).spawned
        assert not tick(board, tid).spawned
    assert kb.get_task(board, tid).status == "ready"
    assert len(kb.list_runs(board, tid)) == 1
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,)).fetchone()[0] == 1


def test_independent_open_own_pr_does_not_inherit_parent_clearance(board):
    tid, _, _ = resolved_parent(board)
    kb.add_comment(board, tid, author="builder", body=f"My independent PR {OWN_PR}")
    def reply(command, **kwargs):
        assert not board.in_transaction
        assert kwargs["timeout"] == 3
        assert kwargs["env"]["GH_CONFIG_DIR"].endswith("isolated-gh")
        return gh_reply("open", False, url=OWN_PR) if command[2].endswith("3165") else gh_reply()
    with patch("subprocess.run", side_effect=reply):
        assert dict(tick(board, tid).respawn_guarded)[tid] == "active_pr"
    assert kb.get_task(board, tid).status == "ready"


def test_fifteen_minute_escalation_is_durable_deduplicated_and_recovers(board):
    tid, parent, _ = resolved_parent(board)
    with patch("subprocess.run", side_effect=OSError("unreachable")):
        assert not tick(board, tid).spawned
        board.execute("UPDATE task_events SET created_at=created_at-899 WHERE task_id=? AND kind='respawn_guarded'", (tid,))
        board.commit()
        with patch("time.time", return_value=kb.time.time()):
            assert not tick(board, tid).spawned
        board.execute("UPDATE task_events SET created_at=created_at-2 WHERE task_id=? AND kind='respawn_guarded'", (tid,))
        board.commit()
        for _ in range(3):
            assert not tick(board, tid).spawned
            kb.add_comment(board, tid, author="coordinator", body="ordinary readback")
    events = [json.loads(r[0]) for r in board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,))]
    assert len(events) == 2
    assert events[-1]["escalated"] and events[-1]["owner"] == "builder"
    assert events[-1]["threshold_seconds"] == 900
    provenance = events[-1]["sources"][PARENT_PR][0]
    assert provenance["parent_task_id"] == parent
    assert provenance["parent_run_id"] == board.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (parent,)).fetchone()[0]
    # A fresh connection models dispatcher restart: no in-memory dedup state.
    with kb.connect() as restarted, patch("subprocess.run", side_effect=OSError("unreachable")):
        assert not tick(restarted, tid).spawned
        assert restarted.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,)).fetchone()[0] == 2
    with patch("subprocess.run", return_value=gh_reply()):
        assert len(tick(board, tid).spawned) == 1


@pytest.mark.parametrize("mutation", ["comment", "owner", "parent", "expired"])
def test_terminal_receipt_cannot_transfer_across_changed_claim_state(board, mutation):
    from hermes_cli.kanban_pr_guard import collect_pr_evidence
    tid, parent, _ = resolved_parent(board)
    with patch("subprocess.run", return_value=gh_reply()):
        receipt = collect_pr_evidence(board, tid)
    if mutation == "comment":
        kb.add_comment(board, tid, author="builder", body=OWN_PR)
    elif mutation == "owner":
        board.execute("UPDATE tasks SET assignee='other' WHERE id=?", (tid,)); board.commit()
    elif mutation == "parent":
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,)); board.commit()
    else:
        receipt["observed_at"] -= 16
    assert kb.claim_task(board, tid, pr_evidence=receipt) is None
    assert len(kb.list_runs(board, tid)) == 1


@pytest.mark.parametrize("state", ["open", "unknown"])
def test_unresolved_episode_cannot_age_into_twenty_four_hour_bypass(board, state):
    tid, _, _ = resolved_parent(board)
    kwargs = {"return_value": gh_reply("open", False)} if state == "open" else {"side_effect": OSError("denied")}
    with patch("subprocess.run", **kwargs):
        assert not tick(board, tid).spawned
        board.execute("UPDATE task_comments SET created_at=created_at-86401 WHERE task_id=?", (tid,))
        board.commit()
        assert not tick(board, tid).spawned
    with patch("subprocess.run", return_value=gh_reply()):
        assert len(tick(board, tid).spawned) == 1


def test_no_profile_auth_cannot_borrow_dispatcher_credentials(board, monkeypatch):
    tid, _, _ = resolved_parent(board)
    monkeypatch.setattr(acceptance, "_gh_env", lambda home: (_ for _ in ()).throw(acceptance._GateAuthError("unavailable")))
    with patch("subprocess.run") as run:
        assert not tick(board, tid).spawned
        run.assert_not_called()


def test_lookup_budget_is_bounded_and_unread_pr_remains_guarded(board):
    tid, _, _ = resolved_parent(board)
    for n in range(10, 17):
        kb.add_comment(board, tid, author="builder", body=f"https://github.com/example/repo/pull/{n}")
    def reply(command, **kwargs):
        assert kwargs["timeout"] == 3 and not board.in_transaction
        return gh_reply(url="https://github.com/example/repo/pull/" + command[2].split("/")[-1])
    with patch("subprocess.run", side_effect=reply) as run:
        assert not tick(board, tid).spawned
        assert run.call_count == 4


def test_full_dispatch_tick_claims_once_after_terminal_read(board):
    tid, _, _ = resolved_parent(board)
    with patch("subprocess.run", return_value=gh_reply()), patch.object(dispatch, "_memory_pressure_level", return_value="ok"):
        first = kb.dispatch_once(board, max_in_progress=1, reconcile_orphans=False, spawn_fn=lambda task, workspace: None)
        second = kb.dispatch_once(board, max_in_progress=1, reconcile_orphans=False, spawn_fn=lambda task, workspace: None)
    assert [s[0] for s in first.spawned] == [tid]
    assert not second.spawned
