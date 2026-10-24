"""Connection owner cancellation uses real SQLite; workers are never launched."""
import json
from contextlib import contextmanager
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_connections import spawn_claimed


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "board.db"
    with kb.connect(path) as conn:
        yield conn, path, {"agent_id": "fresh-agent", "profile": "fresh-profile",
                           "repo": "fresh-repo", "workspace": str(tmp_path / "workspace")}


def task(conn, binding, **kwargs):
    metadata = {"schema": "mrpanache.agent-request.v1", "agent": binding["agent_id"],
                "requested_by": "human:david", "via": "dolly",
                "native_binding": {key: binding[key] for key in ("profile", "repo", "workspace")}}
    metadata.update(kwargs.pop("metadata", {}))
    return kb.create_task(conn, title="bounded work", body=json.dumps(metadata),
                          assignee=binding["profile"], tenant=binding["repo"], **kwargs)


def apply(conn, binding, enabled=False, request_id="disable-1", **kwargs):
    return kb.set_connection_enabled(conn, binding=binding, enabled=enabled,
                                     actor=kwargs.pop("actor", "human:david"), request_id=request_id, **kwargs)


def test_exact_scope_replay_history_and_reenable(board):
    conn, _, binding = board
    own = task(conn, binding)
    other = task(conn, {**binding, "agent_id": "other-agent"})
    other_repo = task(conn, {**binding, "repo": "other-repo"})
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == [own]
    assert receipt["before"] == {"queued": 1, "claimed": 0, "running": 0}
    assert kb.get_task(conn, own).status == "cancelled"
    assert kb.get_task(conn, other).status == kb.get_task(conn, other_repo).status == "ready"
    assert apply(conn, binding) == receipt
    with pytest.raises(ValueError, match="connection_request_conflict"):
        apply(conn, binding, enabled=True)
    fresh = task(conn, binding)
    assert kb.claim_task(conn, fresh) is None
    assert kb.claim_task(conn, other) is not None
    apply(conn, binding, enabled=True, request_id="enable-2")
    assert kb.claim_task(conn, fresh) is not None
    assert kb.get_task(conn, own).status == "cancelled"
    readback = kb.read_connection_lifecycle(conn, binding=binding)
    assert readback["enabled"] is True
    assert readback["latest_receipt"]["request_id"] == "enable-2"
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='cancelled'", (own,)).fetchone()[0] == 1


def test_pending_claim_cancelled_but_legacy_pidless_run_is_running(board):
    conn, _, binding = board
    pending, legacy = task(conn, binding), task(conn, binding)
    pending_task = kb.claim_task(conn, pending, spawn_pending=True)
    kb.claim_task(conn, legacy)
    receipt = apply(conn, binding)
    assert receipt["before"] == {"queued": 0, "claimed": 1, "running": 1}
    assert receipt["cancelled_task_ids"] == [pending]
    assert receipt["running_task_ids"] == [legacy] and receipt["stop_required"]
    calls = []
    assert not spawn_claimed(conn, pending_task, binding["workspace"], lambda *a: calls.append(a), None)["spawned"]
    assert calls == []
    assert kb.heartbeat_claim(conn, pending, claimer=pending_task.claim_lock) is False
    assert kb.complete_task(conn, legacy, result="finished normally", fire_lifecycle_hook=False)
    assert kb.get_task(conn, legacy).status == "done"
    run = conn.execute("SELECT outcome,status FROM task_runs WHERE task_id=?", (pending,)).fetchone()
    assert tuple(run) == ("cancelled", "cancelled")


def test_disable_serializes_with_spawn_and_pid_receipt(board):
    conn, path, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    entered, disable_started, release = threading.Event(), threading.Event(), threading.Event()

    def launch():
        with kb.connect(path) as worker_conn:
            def spawn(*_):
                entered.set()
                assert release.wait(5)
                return 51515
            return spawn_claimed(worker_conn, claimed, binding["workspace"], spawn, None)

    def disable():
        with kb.connect(path) as owner_conn:
            disable_started.set()
            return apply(owner_conn, binding)

    with ThreadPoolExecutor(max_workers=2) as pool:
        spawned = pool.submit(launch)
        assert entered.wait(5)
        disabled = pool.submit(disable)
        assert disable_started.wait(5)
        assert not disabled.done()
        release.set()
        assert spawned.result(timeout=5) == {"spawned": True, "pid": 51515}
        receipt = disabled.result(timeout=5)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier] and receipt["stop_required"]
    assert kb.get_task(conn, identifier).worker_pid == 51515


def test_disable_before_spawn_and_new_review_admission(board):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    apply(conn, binding)
    assert not spawn_claimed(conn, claimed, binding["workspace"], lambda *_: pytest.fail("spawn after disable"), None)["spawned"]
    review = task(conn, binding)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
    assert kb.claim_review_task(conn, review, spawn_pending=True) is None


@pytest.mark.parametrize("metadata", [
    {"requested_by": "human:bram", "via": "dobby"},
    {"requested_by": None, "via": None, "origin_task_id": "foreign-origin"},
])
def test_foreign_owner_rejects_whole_transaction(board, metadata):
    conn, _, binding = board
    own, foreign = task(conn, binding), task(conn, binding, metadata=metadata)
    with pytest.raises(ValueError, match="connection_owner_mismatch"):
        apply(conn, binding)
    assert kb.get_task(conn, own).status == kb.get_task(conn, foreign).status == "ready"
    assert kb.read_connection_lifecycle(conn, binding=binding)["latest_receipt"] is None


def test_cancelled_parent_does_not_promote_or_clean_workspace(board):
    conn, _, binding = board
    workspace = Path(binding["workspace"])
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("preserve work")
    parent = task(conn, binding, workspace_kind="dir", workspace_path=str(workspace))
    child = task(conn, {**binding, "agent_id": "child-agent"}, parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    apply(conn, binding)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert marker.read_text() == "preserve work"
    assert conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (child,)).fetchone()[0] == parent


def test_legacy_unbound_work_is_explicitly_unknown_not_cancelled(board):
    conn, _, binding = board
    legacy = kb.create_task(conn, title="legacy", assignee=binding["profile"])
    receipt = apply(conn, binding)
    assert not receipt["scope_complete"] and receipt["unbound_task_ids"] == [legacy]
    assert receipt["cancelled_task_ids"] == []
    assert kb.get_task(conn, legacy).status == "ready"
    assert not kb.read_connection_lifecycle(conn, binding=binding)["scope_complete"]


def test_spawn_exception_is_not_retried_or_reported_unstarted(board):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    calls = []
    def spawn(*_):
        calls.append(1)
        raise TypeError("could have forked")
    result = spawn_claimed(conn, claimed, binding["workspace"], spawn, None)
    assert not result["spawned"] and result["stop_required"]
    assert calls == [1]
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier]


def test_pid_write_exception_preserves_crossed_spawn_boundary(board, monkeypatch):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    def fail_pid(*_):
        raise RuntimeError("PID receipt failed")
    monkeypatch.setattr(kb, "_set_worker_pid", fail_pid)
    result = spawn_claimed(conn, claimed, binding["workspace"], lambda *_: 31313, None)
    assert result["stop_required"]
    assert apply(conn, binding)["running_task_ids"] == [identifier]


def test_crash_rollback_after_spawn_does_not_restore_pending_proof(board):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    side_effect = []
    class SimulatedCrash(BaseException):
        pass
    def spawn(*_):
        side_effect.append("child exists outside SQLite")
        raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        spawn_claimed(conn, claimed, binding["workspace"], spawn, None)
    # Process death closes and rolls back its connection. Simulate that
    # rollback explicitly; the first committed boundary must remain durable.
    conn.rollback()
    assert side_effect
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier] and receipt["stop_required"]


def test_dispatch_final_guard_after_workspace_resolution(board, monkeypatch):
    from hermes_cli import profiles
    conn, _, binding = board
    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    identifier = task(conn, binding)
    def resolve(*args, **kwargs):
        apply(conn, binding)
        return Path(binding["workspace"])
    monkeypatch.setattr(kb, "resolve_workspace", resolve)
    kb.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("dispatch crossed disabled admission"),
                     reconcile_orphans=False)
    assert kb.get_task(conn, identifier).status == "cancelled"


def test_second_spawn_commit_failure_never_requeues_or_cancels(board, monkeypatch):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    boundary = kb._execute_boundary_with_retry
    commits = []
    def fail_second_commit(connection, sql):
        if sql == "COMMIT":
            commits.append(1)
            if len(commits) == 2:
                raise RuntimeError("simulated commit failure after fork")
        return boundary(connection, sql)
    monkeypatch.setattr(kb, "_execute_boundary_with_retry", fail_second_commit)
    side_effect = []
    outcome = spawn_claimed(conn, claimed, binding["workspace"], lambda *_: side_effect.append(1) or 31313, None)
    assert outcome["stop_required"] and side_effect == [1]
    monkeypatch.setattr(kb, "_execute_boundary_with_retry", boundary)
    assert not spawn_claimed(conn, claimed, binding["workspace"], lambda *_: pytest.fail("replayed uncertain spawn"), None)["spawned"]
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier]


@pytest.mark.parametrize("column,value", [("assignee", "other-profile"), ("tenant", "other-repo"),
                                         ("model_override", "changed-model"), ("body", "changed owner or binding")])
def test_changed_execution_input_never_spawns_stale_snapshot(board, column, value):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    with kb.write_txn(conn):
        # Column is a fixed parameterized test literal, never user input.
        conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, identifier))
    assert not spawn_claimed(conn, claimed, binding["workspace"], lambda *_: pytest.fail("stale execution"), None)["spawned"]


@pytest.mark.parametrize("between_transactions", [False, True])
def test_consistent_rebinding_cannot_start_old_disabled_address(board, monkeypatch, between_transactions):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    other = {**binding, "agent_id": "new-agent", "profile": "new-profile", "repo": "new-repo"}
    metadata = json.loads(claimed.body)
    metadata["agent"] = other["agent_id"]
    metadata["native_binding"] = {key: other[key] for key in ("profile", "repo", "workspace")}
    original_txn = kb.write_txn
    def rebind():
        with original_txn(conn):
            conn.execute("UPDATE tasks SET body=?,assignee=?,tenant=? WHERE id=?",
                         (json.dumps(metadata), other["profile"], other["repo"], identifier))
        apply(conn, binding)
    if between_transactions:
        crossed = []
        @contextmanager
        def after_first_commit(connection, **kwargs):
            with original_txn(connection, **kwargs):
                yield connection
            if not crossed:
                crossed.append(True)
                rebind()
        monkeypatch.setattr(kb, "write_txn", after_first_commit)
    else:
        rebind()
    outcome = spawn_claimed(conn, claimed, binding["workspace"], lambda *_: pytest.fail("spawned disabled old address"), None)
    assert not outcome["spawned"]
    assert kb.get_task(conn, identifier).assignee == other["profile"]


@pytest.mark.parametrize("status", ["ready", "blocked", "review"])
def test_nonterminal_active_run_cannot_be_reported_as_known_zero(board, status):
    conn, _, binding = board
    identifier = task(conn, binding)
    kb.claim_task(conn, identifier)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, identifier))
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier] and receipt["stop_required"]
    assert kb.read_connection_lifecycle(conn, binding=binding)["counts"]["running"] == 1


def test_ttl_requeue_does_not_erase_uncertain_child_or_admit_duplicate(board):
    conn, _, binding = board
    identifier = task(conn, binding)
    claimed = kb.claim_task(conn, identifier, spawn_pending=True)
    calls = []
    def spawn(*_):
        calls.append("child may exist")
        raise RuntimeError("lost PID receipt")
    assert spawn_claimed(conn, claimed, binding["workspace"], spawn, None)["stop_required"]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (identifier,))
    assert kb.release_stale_claims(conn, signal_fn=lambda *_: pytest.fail("PID unknown, no signal")) == 1
    assert kb.get_task(conn, identifier).status == "ready"
    assert kb.claim_task(conn, identifier, spawn_pending=True) is None
    receipt = apply(conn, binding)
    assert receipt["cancelled_task_ids"] == []
    assert receipt["running_task_ids"] == [identifier] and receipt["stop_required"]
    assert calls == ["child may exist"]
    assert kb.read_connection_lifecycle(conn, binding=binding)["counts"]["running"] == 1
