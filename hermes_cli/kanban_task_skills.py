"""Audited, idle-card launch skill correction through the native Kanban contract."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Iterable


_IDLE_STATUSES = frozenset({"todo", "ready", "blocked", "triage"})
_LAUNCH_FENCES = ("claim_lock", "claim_expires", "current_run_id", "worker_pid", "worker_started_at")


def set_task_skills(
    conn: sqlite3.Connection, task_id: str, skills: Iterable[str], *, actor: str, reason: str,
) -> bool:
    """Replace only skills on an idle card; never reclaim, resume or reset failures.

    BEGIN IMMEDIATE serializes validation with dispatcher claims. Keep the guard
    here, not just in CLI, so direct callers retain worker and descendant fences.
    """
    from hermes_cli import kanban_db as kb

    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("kanban set-skills is orchestrator-only; workers cannot change launch metadata")
    if not actor or not actor.strip() or not reason or not reason.strip():
        raise ValueError("actor and a non-empty reason are required for skills audit")
    if isinstance(skills, (str, bytes)):
        raise TypeError("skills must be an iterable of separate skill names")
    values = list(skills)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("each skill name must be a string")
    after = kb._normalize_task_skills(values)
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        active_run = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1", (task_id,),
        ).fetchone()
        if row["status"] not in _IDLE_STATUSES or active_run or any(
            row[field] is not None for field in _LAUNCH_FENCES
        ):
            raise RuntimeError(f"cannot set skills on {task_id}: card is active, terminal, or retains launch fences")
        before = json.loads(row["skills"]) if row["skills"] is not None else None
        if before == after:
            return True
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (json.dumps(after), task_id))
        kb._append_event(conn, task_id, "skills_updated", {
            "before": before, "after": after, "actor": actor, "reason": reason.strip(),
        })
    kb.notify_task_updated(conn, task_id, ("skills",))
    return True


def cmd_set_skills(args: argparse.Namespace) -> int:
    """CLI edge for the same-card kernel mutation; intentionally no force flag."""
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db_connect as kbc

    if os.environ.get("HERMES_KANBAN_TASK"):
        return cli._err("kanban set-skills is orchestrator-only; workers cannot change launch metadata", 2)
    skills = getattr(args, "skills", None)
    clear = getattr(args, "clear", False)
    if bool(skills) == bool(clear):
        return cli._err("kanban set-skills: provide either --skill NAME (repeatable) or --clear", 2)
    try:
        with kbc.connect_closing() as conn:
            ok = set_task_skills(conn, args.task_id, [] if clear else (skills or []),
                                 actor=cli._profile_author(), reason=args.reason)
    except (PermissionError, RuntimeError, TypeError, ValueError) as exc:
        return cli._err(f"kanban: {exc}", 2)
    return cli._ok_or_err(ok, f"no such task: {args.task_id}",
                          f"Updated skills on {args.task_id}; status and launch fences unchanged")
