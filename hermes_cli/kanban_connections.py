"""Owner-side connection admission and cancellation of provably unstarted work.

The existing task event ledger holds admission and operation receipts. A saved
native binding is addressing, never an execution grant. Running workers retain
their existing authority and must be stopped through an explicit owner action.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import re
import time

SCHEMA = "hermes.connection_lifecycle.v1"
_IDS = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_QUEUED = {"triage", "todo", "scheduled", "ready", "blocked", "review"}
_EXECUTION_FIELDS = ("title", "body", "assignee", "tenant", "workspace_kind", "skills",
                     "model_override", "provider_override", "reasoning_effort", "goal_mode",
                     "goal_max_turns", "max_runtime_seconds", "max_retries", "project_id",
                     "session_id", "workflow_template_id", "current_step_key")


def _same_execution(current, claimed, workspace):
    return (all(getattr(current, key) == getattr(claimed, key) for key in _EXECUTION_FIELDS)
            and (current.workspace_path is None or current.workspace_path == workspace))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _binding(value):
    keys = {"agent_id", "profile", "repo", "workspace"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("connection_binding_invalid")
    if not all(isinstance(value[k], str) and _IDS.fullmatch(value[k]) for k in keys - {"workspace"}):
        raise ValueError("connection_binding_invalid")
    workspace = value["workspace"]
    if (not isinstance(workspace, str) or not Path(workspace).is_absolute()
            or re.search(r"[\0\r\n]", workspace) or os.path.normpath(workspace) != workspace):
        raise ValueError("connection_binding_invalid")
    return dict(value)


def task_binding(task):
    """The task's original address must still match its native owner and tenant."""
    try:
        metadata = json.loads((task.body or "").splitlines()[0])
    except (ValueError, IndexError):
        return None
    if not isinstance(metadata, dict) or "native_binding" not in metadata:
        return None
    saved = metadata["native_binding"]
    if not isinstance(saved, dict):
        raise ValueError("connection_binding_invalid")
    binding = _binding({"agent_id": metadata.get("agent"),
                        **{key: saved.get(key) for key in ("profile", "repo", "workspace")}})
    if metadata.get("schema") != "mrpanache.agent-request.v1" or task.assignee != binding["profile"] or task.tenant != binding["repo"]:
        raise ValueError("connection_task_scope_changed")
    return binding


def _namespace(binding):
    return "connection:" + _digest(binding)


def _owned_by_actor(task, actor):
    metadata = json.loads(task.body.splitlines()[0])
    owner = (metadata.get("requested_by"), metadata.get("via"))
    if owner == (None, None) and not any(key in metadata for key in
            ("origin_request_id", "origin_conversation_id", "origin_task_id")):
        owner = ("human:david", "dolly")
    return actor == "human:david" and owner == (actor, "dolly")


def _scoped_tasks(conn, binding):
    from hermes_cli import kanban_db as kb

    tasks, unbound = [], []
    # Same-profile legacy work cannot safely be attributed to this repo.
    for row in conn.execute("SELECT id FROM tasks WHERE assignee=? ORDER BY created_at,id",
                            (binding["profile"],)).fetchall():
        task = kb.get_task(conn, row["id"])
        try:
            saved = task_binding(task)
        except ValueError:
            saved = None
        if saved == binding:
            tasks.append(task)
        elif saved is None and task.status not in {"done", "archived", "cancelled"}:
            unbound.append(task.id)
    return tasks, unbound


def _snapshot(conn, binding):
    tasks, unbound = _scoped_tasks(conn, binding)
    counts = {phase: sum(_phase(conn, task) == phase for task in tasks)
              for phase in ("queued", "claimed", "running")}
    running = [task.id for task in tasks if _phase(conn, task) == "running"]
    return tasks, {"counts": counts, "running_task_ids": running,
                   "unbound_task_ids": unbound, "scope_complete": not unbound}


def read_connection_lifecycle(conn, *, binding):
    """Read the current durable owner state without claiming legacy scope."""
    binding = _binding(binding)
    # A read transaction gives admission, task counts and receipt one snapshot.
    if conn.in_transaction:
        raise RuntimeError("connection_read_requires_own_transaction")
    conn.execute("BEGIN")
    try:
        _, snapshot = _snapshot(conn, binding)
        admission = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='connection_admission' ORDER BY id DESC LIMIT 1",
                                 (_namespace(binding),)).fetchone()
        latest = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='connection_lifecycle_receipt' ORDER BY id DESC LIMIT 1",
                              (_namespace(binding),)).fetchone()
        enabled = admission is None or json.loads(admission["payload"])["enabled"]
        return {"schema_version": SCHEMA, "binding": binding, "enabled": enabled,
                **snapshot, "stop_required": not enabled and bool(snapshot["running_task_ids"]),
                "latest_receipt": {**json.loads(latest["payload"]), "event_id": latest["id"]} if latest else None}
    finally:
        conn.rollback()


def task_admitted(conn, task):
    if task is None:
        return False
    if task.status != "running" and _unresolved_spawn(conn, task.id):
        return False
    try:
        binding = task_binding(task)
    except ValueError:
        return False
    if binding is None:
        return True  # Unrelated and legacy native work keeps its existing admission.
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='connection_admission' ORDER BY id DESC LIMIT 1",
                       (_namespace(binding),)).fetchone()
    return row is None or json.loads(row["payload"])["enabled"] is True


def _unresolved_spawn(conn, task_id):
    # Legacy TTL recovery may restore ready after losing a PID receipt. Its
    # requeue is not proof that a child from our durable boundary has exited.
    return conn.execute("""
        SELECT 1 FROM task_events e JOIN task_runs r ON r.id=e.run_id
        WHERE e.task_id=? AND e.kind='spawn_started'
          AND json_extract(e.payload,'$.schema')=?
          AND (r.ended_at IS NULL OR r.outcome IN ('reclaimed','spawn_failed','gave_up'))
          AND NOT EXISTS (SELECT 1 FROM task_events s WHERE s.task_id=e.task_id
                          AND s.run_id=e.run_id AND s.kind='spawned'
                          AND json_extract(s.payload,'$.pid') > 0)
        LIMIT 1""", (task_id, SCHEMA)).fetchone() is not None


def _phase(conn, task):
    if task.status not in {"done", "archived", "cancelled"} and _unresolved_spawn(conn, task.id):
        return "running"
    if task.status in _QUEUED and task.current_run_id is None and task.worker_pid is None:
        return "queued"
    if task.status not in {"done", "archived", "cancelled", "running"} and (task.current_run_id is not None or task.worker_pid is not None):
        return "running"
    if task.status != "running":
        return "terminal"
    # Old PID-less runs may already have forked. Only the new dispatcher
    # receipt proves a claim has not crossed the serialized spawn boundary.
    pending = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='spawn_pending' AND json_extract(payload,'$.schema')=? LIMIT 1",
                           (task.id, task.current_run_id, SCHEMA)).fetchone()
    launched = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind IN ('spawn_started','spawned') LIMIT 1",
                            (task.id, task.current_run_id)).fetchone()
    return "claimed" if pending and not launched and task.worker_pid is None else "running"


def _cancel(conn, task, actor, request_id):
    from hermes_cli import kanban_db as kb

    run_id = task.current_run_id
    metadata = None
    if run_id is not None:
        row = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
        metadata["cancellation"] = {"actor": actor, "request_id": request_id, "schema": SCHEMA}
    kb._end_run(conn, task.id, outcome="cancelled", status="cancelled",
                summary="Connection disabled before worker start", metadata=metadata)
    conn.execute("UPDATE tasks SET status='cancelled', completed_at=?, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                 (int(time.time()), task.id))
    kb._append_event(conn, task.id, "cancelled", {"schema": SCHEMA, "actor": actor, "request_id": request_id,
                                                  "reason": "connection_disabled", "no_execution": True}, run_id=run_id)


def set_connection_enabled(conn, *, binding, enabled, actor, request_id):
    """One atomic admission change, scoped cancellations and idempotent receipt.

    Cancelled parents remain unsatisfied; cancellation never promotes children,
    cleans a workspace or signals a worker. Existing running work is read back.
    """
    from hermes_cli import kanban_db as kb

    binding = _binding(binding)
    if type(enabled) is not bool or actor != "human:david" or not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("connection_request_invalid")
    namespace = _namespace(binding)
    payload = {"binding": binding, "enabled": enabled, "actor": actor, "request_id": request_id}
    fingerprint = _digest(payload)
    with kb.write_txn(conn):
        previous = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='connection_lifecycle_receipt' AND json_extract(payload,'$.request_id')=? ORDER BY id DESC LIMIT 1",
                                (namespace, request_id)).fetchone()
        if previous:
            receipt = json.loads(previous["payload"])
            if receipt["fingerprint"] != fingerprint:
                raise ValueError("connection_request_conflict")
            return {**receipt, "event_id": previous["id"]}
        tasks, snapshot = _snapshot(conn, binding)
        if any(not _owned_by_actor(task, actor) for task in tasks
               if task.status not in {"done", "archived", "cancelled"}):
            raise ValueError("connection_owner_mismatch")
        before = snapshot["counts"]
        kb._append_event(conn, namespace, "connection_admission", payload)
        cancelled = []
        if not enabled:
            for task in tasks:
                if _phase(conn, task) in ("queued", "claimed"):
                    _cancel(conn, task, actor, request_id)
                    cancelled.append(task.id)
        running = [task.id for task in tasks if _phase(conn, kb.get_task(conn, task.id)) == "running"]
        receipt = {"schema_version": SCHEMA, **payload, "fingerprint": fingerprint,
                   "before": before, "after": {"queued": before["queued"] if enabled else 0,
                                                "claimed": before["claimed"] if enabled else 0, "running": len(running)},
                   "cancelled_task_ids": cancelled, "running_task_ids": running,
                   "unbound_task_ids": snapshot["unbound_task_ids"], "scope_complete": snapshot["scope_complete"],
                   "stop_required": not enabled and bool(running), "observed_at": int(time.time())}
        kb._append_event(conn, namespace, "connection_lifecycle_receipt", receipt)
        return {**receipt, "event_id": conn.execute("SELECT last_insert_rowid()").fetchone()[0]}


def spawn_claimed(conn, task, workspace, spawn_fn, board, *, extra_skills=()):
    """Durably cross the unstarted boundary before serializing spawn and PID.

    A process crash or failed commit after fork must never restore proof of an
    unstarted claim. The first commit records uncertainty; the second write
    transaction rechecks admission and serializes OS spawn with cancellation.
    """
    from hermes_cli import kanban_db as kb

    try:
        board_parameter = "board" in inspect.signature(spawn_fn).parameters
    except (TypeError, ValueError):
        board_parameter = False
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        if (not current or current.status != "running" or current.current_run_id != task.current_run_id
                or current.claim_lock != task.claim_lock or _phase(conn, current) != "claimed"
                or not _same_execution(current, task, workspace)):
            return {"spawned": False, "pid": None}
        if not task_admitted(conn, current):
            if _phase(conn, current) == "claimed":
                _cancel(conn, current, "native-dispatcher", "disabled-before-spawn")
            return {"spawned": False, "pid": None}
        kb._append_event(conn, task.id, "spawn_started", {"schema": SCHEMA}, run_id=current.current_run_id)
    try:
        with kb.write_txn(conn):
            current = kb.get_task(conn, task.id)
            if (not current or current.status != "running" or current.current_run_id != task.current_run_id
                    or current.claim_lock != task.claim_lock or not task_admitted(conn, current)
                    or not _same_execution(current, task, workspace)):
                return {"spawned": False, "pid": None,
                        "stop_required": bool(current and current.status == "running")}
            spawn_task = replace(current, skills=list(dict.fromkeys([*(current.skills or []), *extra_skills]))) if extra_skills else current
            pid = spawn_fn(spawn_task, workspace, board=board) if board_parameter else spawn_fn(spawn_task, workspace)
            if pid:
                kb._set_worker_pid(conn, task.id, int(pid))
        return {"spawned": True, "pid": pid}
    except Exception:
        # Fork, PID persistence, or COMMIT may fail after a child exists.
        # The earlier durable boundary remains; do not let the dispatcher's
        # generic spawn-failure handler requeue this uncertain native run.
        return {"spawned": False, "pid": None, "stop_required": True}
