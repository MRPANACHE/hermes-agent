"""Native Kanban idempotency and admitted continuation receipts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


def _dedupe_active_idempotency_keys(conn: sqlite3.Connection) -> None:
    """Archive older active duplicates before creating the unique index.

    Boards created before the race fix may already contain duplicate active
    rows. Keep the row that the existing idempotency lookup resolves to and
    retain every duplicate row and event; only its lifecycle status changes.
    The ``archived`` event is durable migration evidence and mirrors the
    normal archive lifecycle, including closing a leaked active run.
    """
    from hermes_cli.kanban_db import _IDEMPOTENCY_KEY_SQL_EXPR, _append_event, _end_run
    duplicate_keys = conn.execute(
        f"SELECT {_IDEMPOTENCY_KEY_SQL_EXPR} AS canonical_key FROM tasks "
        f"WHERE idempotency_key IS NOT NULL AND {_IDEMPOTENCY_KEY_SQL_EXPR} != '' "
        "AND status != 'archived' "
        f"GROUP BY {_IDEMPOTENCY_KEY_SQL_EXPR} HAVING COUNT(*) > 1"
    ).fetchall()
    if not duplicate_keys:
        return

    task_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
    }
    runs_exist = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_runs'"
    ).fetchone() is not None
    can_close_runs = "current_run_id" in task_columns and runs_exist

    for key_row in duplicate_keys:
        key = key_row["canonical_key"]
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE {_IDEMPOTENCY_KEY_SQL_EXPR} = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC, id DESC",
            (key,),
        ).fetchall()
        for duplicate in rows[1:]:
            task_id = duplicate["id"]
            run_id = None
            if can_close_runs:
                run_id = _end_run(
                    conn,
                    task_id,
                    outcome="reclaimed",
                    status="reclaimed",
                    summary="task archived during idempotency-key migration",
                )
            conn.execute(
                "UPDATE tasks SET status = 'archived', "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status != 'archived'",
                (task_id,),
            )
            _append_event(
                conn,
                task_id,
                "archived",
                {
                    "reason": "duplicate_idempotency_key_migration",
                    "idempotency_key": key,
                },
                run_id=run_id,
            )


def _canonical_idempotency_key(value: Optional[str]) -> Optional[str]:
    """Normalize supported ASCII surrounding whitespace, preserving other text."""
    from hermes_cli.kanban_db import _IDEMPOTENCY_KEY_WHITESPACE
    if value is None:
        return None
    return str(value).strip(_IDEMPOTENCY_KEY_WHITESPACE) or None


def _budget_assignment(task: Task) -> dict:
    """The task authority and execution limits a continuation must retain."""
    return {key: getattr(task, key) for key in (
        "title", "body", "assignee", "workspace_kind", "workspace_path", "branch_name",
        "goal_mode", "goal_max_turns", "max_runtime_seconds", "max_retries",
        "model_override", "provider_override", "reasoning_effort")}


def goal_budget_history(conn: sqlite3.Connection, task_id: str) -> dict:
    """Read use of this interrupted attempt and its continuations, not older work."""
    from hermes_cli.kanban_db import list_runs
    turns, runtime, usage = 0, 0, {}
    runs = list_runs(conn, task_id)
    frontier = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=? "
                            "AND kind IN ('review_requested','review_reopened','changes_requested',"
                            "'completed','status','scheduled')", (task_id,)).fetchone()[0]
    admission = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND id>? "
                             "AND kind='budget_continued' ORDER BY id DESC LIMIT 1", (task_id, frontier)).fetchone()
    start = json.loads(admission["payload"]).get("chain_start_run") if admission else None
    latest_budget = next((r for r in reversed(runs) if (r.metadata or {}).get("goal_budget") and conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND id>? AND kind='blocked'",
        (task_id, r.id, frontier)).fetchone()), None)
    if latest_budget:
        start = latest_budget.metadata["goal_budget"].get("chain_start_run", latest_budget.id)
    start = start or (runs[-1].id if runs else 0)
    for run in runs:
        if run.id < start:
            continue
        if run.ended_at is not None:
            runtime += max(0, run.ended_at - run.started_at)
        budget = (run.metadata or {}).get("goal_budget", {})
        historical = re.fullmatch(r"Goal-mode worker exhausted its turn budget \((\d+)/(\d+)\) "
                                  r"without completing the task\. Last judge verdict: .*", run.summary or "", re.S)
        turns += int(budget.get("turns_used", int(historical[1]) if historical else 0))
        for key, value in budget.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    return {"chain_start_run": start, "active_chain": bool(admission or latest_budget),
            "turns_used": turns, "runtime_seconds": runtime, "usage": usage}


def block_goal_budget(conn: sqlite3.Connection, task_id: str, *, expected_run_id: int,
                      reason: str, turns_used: int, last_response: str,
                      session_id: Optional[str] = None, usage: Optional[dict] = None) -> bool:
    """Store an actual loop exhaustion, its checkpoint and native run together."""
    from hermes_cli.kanban_db import _budget_assignment, block_task, get_task, goal_budget_history, write_txn
    with write_txn(conn):
        task = get_task(conn, task_id)
        if not task or task.current_run_id != expected_run_id:
            return False
        history = goal_budget_history(conn, task_id)
        budget = {"schema": "hermes.goal-budget.v1", "turns_used": turns_used,
                  "chain_start_run": history["chain_start_run"],
                  "last_response": last_response, "session_id": session_id,
                  "usage": usage or {}, "assignment": _budget_assignment(task)}
        return block_task(conn, task_id, reason=reason, kind="turn_budget",
                          expected_run_id=expected_run_id, metadata={"goal_budget": budget},
                          allow_nested=True)


def goal_budget_binding(conn: sqlite3.Connection, task_id: str) -> dict:
    """Admit only the current proven turn exhaustion, never a low turn setting."""
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT, DEFAULT_FAILURE_LIMIT, _budget_assignment, _parents_satisfied, _pid_alive, get_task, goal_budget_history, list_runs
    def deny(reason: str) -> None:
        raise ValueError(reason)

    task = get_task(conn, task_id)
    if not task or task.status != "blocked" or task.current_run_id is not None:
        deny("budget_block_changed")
    if task.claim_lock or task.worker_pid is not None or not task.goal_mode:
        deny("budget_worker_exit_not_confirmed")
    runs = list_runs(conn, task_id)
    if not runs or any(r.ended_at is None for r in runs):
        deny("budget_worker_exit_not_confirmed")
    last = runs[-1]
    spawned = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                           "AND kind='spawned' ORDER BY id DESC LIMIT 1", (task_id, last.id)).fetchone()
    if spawned:
        pid = json.loads(spawned["payload"] or "{}").get("pid")
        if type(pid) is not int or pid <= 0:
            deny("budget_worker_exit_not_confirmed")
        if _pid_alive(pid):
            deny("budget_worker_exit_not_confirmed")
    row = conn.execute("SELECT id,run_id,payload FROM task_events WHERE task_id=? "
                       "AND kind='blocked' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row or row["run_id"] != last.id or last.outcome != "blocked" or last.profile != task.assignee:
        deny("budget_run_changed")
    payload = json.loads(row["payload"] or "{}")
    budget = (last.metadata or {}).get("goal_budget", {})
    if budget.get("schema") == "hermes.goal-budget.v1":
        if task.block_kind != "turn_budget" or payload.get("kind") != "turn_budget":
            deny("budget_block_changed")
        if budget.get("assignment") != _budget_assignment(task):
            deny("budget_assignment_changed")
    else:
        # Historical inference requires the native run's exact exhaustion
        # receipt AND its linked block event; goal_max_turns is not evidence.
        match = re.fullmatch(r"Goal-mode worker exhausted its turn budget \((\d+)/(\d+)\) "
                             r"without completing the task\. Last judge verdict: .*", last.summary or "", re.S)
        if (task.block_kind is not None or payload.get("kind") is not None or not match
                or int(match[1]) < int(match[2]) or payload.get("reason") != last.summary):
            deny("budget_execution_proof_missing")
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? "
                    "AND kind NOT IN ('commented','mrpanache_clarified','respawn_guarded') LIMIT 1",
                    (task_id, row["id"])).fetchone():
        deny("budget_control_changed")
    if not _parents_satisfied(conn, task_id):
        deny("budget_parents_not_complete")
    if task.consecutive_failures >= (task.max_retries if task.max_retries is not None else DEFAULT_FAILURE_LIMIT):
        deny("budget_retry_limit_reached: explicit operator action required")
    if task.block_recurrences >= BLOCK_RECURRENCE_LIMIT:
        deny("budget_continuation_limit_reached: explicit operator action required")
    history = goal_budget_history(conn, task_id)
    history["active_chain"] = True
    if task.max_runtime_seconds is not None and history["runtime_seconds"] >= task.max_runtime_seconds:
        deny("budget_runtime_limit_reached: explicit operator action required")
    return {"schema": "hermes.budget-continuation.v1", "task_id": task_id,
            "expected_run_id": last.id, "block_event_id": row["id"],
            "chain_start_run": history["chain_start_run"],
            "consecutive_failures": task.consecutive_failures,
            "block_recurrences": task.block_recurrences,
            "assignment": _budget_assignment(task), "history": history}


def goal_budget_admitted(conn: sqlite3.Connection, task_id: str) -> bool:
    """Revalidate a supervisor's single admission under the native claim lock."""
    from hermes_cli.kanban_db import _budget_assignment, get_task, goal_budget_history
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                       "AND kind='budget_continued' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return False
    binding = json.loads(row["payload"])
    task = get_task(conn, task_id)
    if (not task or task.status != "ready" or _budget_assignment(task) != binding["assignment"]
            or task.consecutive_failures != binding["consecutive_failures"]
            or task.block_recurrences != binding["block_recurrences"]):
        return False
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? "
                    "AND kind NOT IN ('commented','mrpanache_resumed','respawn_guarded') LIMIT 1",
                    (task_id, row["id"])).fetchone():
        return False
    return goal_budget_history(conn, task_id) == binding["history"]


def parse_schedule_time(value: str) -> int:
    """Convert an ISO timestamp with an explicit timezone to epoch seconds."""
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--at requires an ISO timestamp with timezone, e.g. 2026-10-07T11:15:00+02:00") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("--at requires an explicit timezone offset or Z")
    return int(timestamp.timestamp())


def wake_scheduled_tasks(conn: sqlite3.Connection) -> list[str]:
    """Consume only explicit due schedules; ordinary blocked cards stay put."""
    from hermes_cli.kanban_db import _append_event, _pid_alive, unblock_task, write_txn
    now = int(time.time())
    awakened = []
    with write_txn(conn):
        rows = conn.execute(
            "SELECT t.id, t.worker_pid, e.id AS event_id, e.run_id, e.payload "
            "FROM tasks t JOIN task_events e ON e.id = ("
            "SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind = 'scheduled') "
            "WHERE t.status = 'scheduled' AND t.claim_lock IS NULL "
            "AND t.current_run_id IS NULL ORDER BY e.id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            wake_at = payload.get("wake_at")
            if type(wake_at) is not int or wake_at > now:
                continue
            original_pid = payload.get("worker_pid")
            if original_pid is not None and type(original_pid) is not int:
                continue
            if _pid_alive(row["worker_pid"]) or _pid_alive(original_pid):
                continue
            # Savepoint composes the existing run/parent/phase transition with
            # its durable wake receipt. Leaving 'scheduled' consumes the timer.
            if unblock_task(conn, row["id"], allow_nested=True):
                _append_event(
                    conn, row["id"], "schedule_woke",
                    {"schedule_event_id": row["event_id"], "wake_at": wake_at},
                    run_id=row["run_id"],
                )
                awakened.append(row["id"])
    return awakened


def ready_resume_binding(conn, task_id, *, before_event=None, before_comment=None):
    """Read-only, fail-closed binding for an explicit same-task ready decision.

    The authenticated ingress records this snapshot in its existing resumed
    receipt. No prose is classified and no task state is repaired here.
    Optional cutoffs let the guard compare the pre-decision history exactly.
    """
    from hermes_cli.kanban_db import get_task, list_runs, parent_ids
    def deny(reason):
        raise ValueError(reason)

    task = get_task(conn, task_id)
    if not task or task.status != "ready" or task.block_kind != "needs_input":
        deny("ready_not_resumable")
    if task.current_run_id is not None or task.claim_lock or task.worker_pid is not None:
        deny("worker_exit_not_confirmed")
    runs = list_runs(conn, task_id)
    if not runs or any(r.ended_at is None for r in runs):
        deny("worker_exit_not_confirmed")
    last = runs[-1]
    if last.profile != task.assignee or last.outcome != "blocked" or task.consecutive_failures:
        deny("ready_run_changed")
    if conn.execute("SELECT 1 FROM task_links l LEFT JOIN tasks p ON p.id=l.parent_id "
                    "WHERE l.child_id=? AND (p.id IS NULL OR p.status NOT IN ('done','archived')) LIMIT 1",
                    (task_id,)).fetchone():
        deny("parents_not_complete")
    events = [dict(r) for r in conn.execute(
        "SELECT id,kind,run_id,payload FROM task_events WHERE task_id=? ORDER BY id", (task_id,))
        if r["kind"] != "respawn_guarded" and (before_event is None or r["id"] < before_event)]
    comments = [dict(r) for r in conn.execute(
        "SELECT id,author,body FROM task_comments WHERE task_id=? ORDER BY id", (task_id,))
        if before_comment is None or r["id"] < before_comment]
    def payload(e):
        try:
            p = json.loads(e["payload"] or "{}")
        except (TypeError, ValueError):
            deny("ready_lineage_invalid")
        if not isinstance(p, dict):
            deny("ready_lineage_invalid")
        return p
    block = next((e for e in reversed(events) if e["kind"] in ("blocked", "block_loop_detected")), None)
    resume = next((e for e in reversed(events) if e["kind"] == "mrpanache_resumed"
                   and "ready_admission" not in payload(e)), None)
    claim = next((e for e in reversed(events) if e["kind"] == "claimed"), None)
    if not block or not resume or not claim:
        deny("ready_lineage_invalid")
    rp = payload(resume)
    if not (payload(block).get("kind") == "needs_input" and claim["run_id"] == last.id
            and block["run_id"] == last.id
            and claim["id"] < block["id"] < resume["id"]
            and rp.get("previous_run_id") == last.id and rp.get("block_event_id") == block["id"]
            and rp.get("action") == "resume"
            and isinstance(rp.get("request_id"), str) and rp.get("request_id")
            and isinstance(rp.get("fingerprint"), str) and len(rp["fingerprint"]) == 64
            and any(e["kind"] == "unblocked" and block["id"] < e["id"] < resume["id"] for e in events)):
        deny("ready_lineage_invalid")
    if any(e["kind"] in ("claimed", "blocked", "block_loop_detected", "review_requested",
                          "changes_requested", "status", "reclaimed", "unblocked", "promoted")
           and e["id"] > resume["id"] for e in events):
        deny("ready_lineage_changed")
    rc = next((c for c in comments if c["id"] == rp.get("comment_id")), None)
    if not rc or rc["author"] != "mrpanache-mcp":
        deny("ready_lineage_invalid")
    # All recorded historical workers must be absent. Unknown PID identity or
    # permission is not exit; never signal/reclaim a process from this route.
    pids = [r.worker_pid for r in runs if r.worker_pid is not None]
    spawns = [e for e in events if e["kind"] == "spawned"]
    if not any(e["run_id"] == last.id for e in spawns):
        deny("worker_exit_not_confirmed")
    pids.extend(payload(e).get("pid") for e in spawns)
    import psutil
    for pid in pids:
        if type(pid) is not int or pid <= 0:
            deny("worker_exit_not_confirmed")
        try:
            exists = psutil.pid_exists(pid)
        except (OSError, psutil.Error):
            deny("worker_exit_not_confirmed")
        if exists:
            deny("worker_exit_not_confirmed")
    state = {name: getattr(task, name) for name in (
        "id", "body", "assignee", "status", "block_kind", "workspace_kind", "workspace_path",
        "branch_name", "current_run_id", "claim_lock", "worker_pid", "max_runtime_seconds",
        "consecutive_failures", "goal_mode")}
    snapshot = {"task": state, "runs": [dict(r) for r in conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,))],
        "parents": parent_ids(conn, task_id), "events": events, "comments": comments}
    sha = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()).hexdigest()
    return {"schema": "hermes.ready-resume.v1", "task_id": task_id, "owner": task.assignee,
            "expected_run_id": last.id, "block_event_id": block["id"],
            "resume_event_id": resume["id"], "expected_event_id": events[-1]["id"],
            "binding_sha256": sha}


def ready_resume_admitted(conn, task_id):
    """Consume only a fresh authenticated resumed receipt, never a clarify."""
    from hermes_cli.kanban_db import ready_resume_binding
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                       "AND kind='mrpanache_resumed' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return False
    try:
        p = json.loads(row["payload"])
        admission = p["ready_admission"]
        comment = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND id=?",
                               (task_id, p["comment_id"])).fetchone()
        ce = admission["decision_comment_event_id"]
        frontier = conn.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? "
                                "AND kind!='respawn_guarded' ORDER BY id DESC LIMIT 2", (task_id,)).fetchall()
        if (len(frontier) != 2 or frontier[0]["id"] != row["id"] or frontier[1]["id"] != ce
                or frontier[1]["kind"] != "commented" or not comment
                or comment["author"] != "mrpanache-mcp" or p.get("action") != "resume"
                or p.get("previous_run_id") != admission["binding"]["expected_run_id"]
                or p.get("block_event_id") != admission["binding"]["block_event_id"]
                or not isinstance(p.get("request_id"), str)
                or not isinstance(p.get("fingerprint"), str) or len(p["fingerprint"]) != 64
                or hashlib.sha256(comment["body"].encode()).hexdigest() != admission["comment_sha256"]):
            return False
        if conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND id>? LIMIT 1",
                        (task_id, comment["id"])).fetchone():
            return False
        return ready_resume_binding(conn, task_id, before_event=ce,
                                    before_comment=comment["id"]) == admission["binding"]
    except (ValueError, KeyError, TypeError):
        return False


def _initial_parent_pr_reference(
    conn: sqlite3.Connection, task_id: str, comment: sqlite3.Row,
) -> bool:
    """Recognize an ended parent's delivery, never a child's existing PR.

    Only a pristine, parent-created child qualifies. Every URL must be bound
    to a linked parent's latest completed run, by its original owner, before
    completion. No state is changed; the ordinary claim still owns admission.
    """
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (not task or task["status"] != "ready" or task["started_at"] is not None
            or task["claim_lock"] is not None or task["current_run_id"] is not None
            or task["worker_pid"] is not None or comment["author"] != task["created_by"]
            or comment["author"] == task["assignee"]
            or conn.execute("SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (task_id,)).fetchone()):
        return False
    created = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        original = json.loads(created["payload"]) if created else None
    except (TypeError, ValueError):
        return False
    if not isinstance(original, dict) or original.get("assignee") != task["assignee"]:
        return False
    parents = conn.execute(
        "SELECT p.status,p.assignee,r.profile,r.outcome,r.ended_at,r.metadata "
        "FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "LEFT JOIN task_runs r ON r.id=(SELECT MAX(id) FROM task_runs WHERE task_id=p.id) "
        "WHERE l.child_id=?", (task_id,),
    ).fetchall()
    if not parents or any(p["status"] not in ("done", "archived") for p in parents):
        return False
    delivered = set()
    for parent in parents:
        if (parent["assignee"] != comment["author"] or parent["profile"] != comment["author"]
                or parent["outcome"] != "completed" or parent["ended_at"] is None
                or comment["created_at"] > parent["ended_at"]):
            continue
        try:
            metadata = json.loads(parent["metadata"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(metadata, dict) and isinstance(metadata.get("pr_url"), str):
            delivered.add(metadata["pr_url"])
    urls = set(_RESPAWN_GUARD_PR_URL_RE.findall(comment["body"] or ""))
    return bool(urls) and urls.issubset(delivered)


@dataclass(frozen=True)
class WorkerCommandConfig:
    command: tuple[str, ...]
    completion_mode: str


def _resolve_worker_config(
    hermes_home: Optional[str],
) -> Optional[WorkerCommandConfig]:
    """Resolve a named profile's fixed ``worker.command`` argv.

    The profile config is read directly so malformed YAML cannot be swallowed
    by the normal config loader and silently fall back to a native agent.  A
    command is deliberately profile-scoped and absolute-only: card content,
    task fields, and the task workspace never participate in executable
    selection.  The parsed list is passed literally; Hermes performs no shell,
    environment-variable, or backslash expansion.
    """
    from hermes_cli.kanban_db import WorkerCommandConfig, _IS_WINDOWS, _WORKER_COMPLETION_MODES
    if not hermes_home:
        return None
    config_path = os.path.join(hermes_home, "config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            raw_text = config_file.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            f"worker.command: profile config {config_path!r} is unreadable: {exc}"
        ) from exc

    from utils import fast_safe_load

    try:
        parsed = fast_safe_load(raw_text)
    except Exception as exc:
        raise RuntimeError(
            f"worker.command: {config_path!r} does not parse as YAML; refusing "
            "to fall back to the native agent"
        ) from exc
    if not isinstance(parsed, dict):
        return None
    worker_cfg = parsed.get("worker")
    raw = worker_cfg.get("command") if isinstance(worker_cfg, dict) else None
    if raw is None:
        return None

    home_real = os.path.realpath(hermes_home)
    if os.path.basename(os.path.dirname(home_real)) != "profiles":
        raise RuntimeError(
            "worker.command must be declared in a named profile's config.yaml, "
            "not the root config"
        )
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(part, str) and part for part in raw)
    ):
        raise RuntimeError(
            "worker.command must be a non-empty list of non-empty argv strings"
        )
    head = raw[0]
    if not os.path.isabs(head):
        raise RuntimeError(
            "worker.command argv[0] must be an absolute executable path; "
            f"relative or bare value {head!r} is not trusted"
        )
    if not os.path.isfile(head) or (
        not _IS_WINDOWS and not os.access(head, os.X_OK)
    ):
        raise RuntimeError(
            f"worker.command executable is missing or not executable: {head!r}"
        )
    completion_mode = worker_cfg.get("completion_mode", "exit_code")
    if (
        not isinstance(completion_mode, str)
        or completion_mode not in _WORKER_COMPLETION_MODES
    ):
        raise RuntimeError(
            "worker.completion_mode must be one of "
            f"{sorted(_WORKER_COMPLETION_MODES)}"
        )
    return WorkerCommandConfig(
        command=tuple(raw),
        completion_mode=completion_mode,
    )


def _resolve_worker_command(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Back-compatible command-only view of the profile worker config."""
    from hermes_cli.kanban_db import _resolve_worker_config
    config = _resolve_worker_config(hermes_home)
    return list(config.command) if config is not None else None
