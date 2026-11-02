"""Apply independently measured feedback to an idle task, never a new worker rail."""
from __future__ import annotations

import json
import sys


def apply_measured_outcome(conn, *, task_id, profile, expected_run_id, action,
                           evidence_ref, evidence_refs, reason):
    """The caller owns source authentication; compare-and-swap pins its run.

    Replays keep the original receipt. Newer work, human blockers and active
    claims are never overwritten by an old measurement.
    """
    from hermes_cli import kanban_db as kb

    if action not in {"close", "reopen"} or not evidence_ref or not evidence_refs:
        raise ValueError("measured_outcome_invalid")
    with kb.write_txn(conn):
        known = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                             "AND kind='measured_outcome' AND json_extract(payload,'$.evidence_ref')=?",
                             (task_id, evidence_ref)).fetchone()
        if known:
            payload = json.loads(known["payload"])
            if payload["action"] != action or payload["expected_run_id"] != expected_run_id:
                raise ValueError("measured_outcome_conflict")
            return {"applied": True, "replay": True, "event_id": known["id"],
                    "status": kb.get_task(conn, task_id).status}
        task = kb.get_task(conn, task_id)
        latest = conn.execute("SELECT id,profile FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                              (task_id,)).fetchone()
        if (not task or not latest or latest["id"] != expected_run_id or latest["profile"] != profile
                or task.assignee != profile or task.status not in {"done", "scheduled"}
                or task.current_run_id is not None or task.worker_pid is not None or task.claim_lock is not None):
            return {"applied": False, "reason": "native_task_changed_or_active"}
        if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND (ended_at IS NULL OR worker_pid IS NOT NULL)",
                        (task_id,)).fetchone():
            return {"applied": False, "reason": "native_run_active"}
        # Reopening a satisfied parent invalidates downstream work via the native
        # primitive; do not kill a live descendant from measurement delivery.
        if action == "reopen" and conn.execute(
                "WITH RECURSIVE d(id) AS (SELECT child_id FROM task_links WHERE parent_id=? "
                "UNION SELECT l.child_id FROM task_links l JOIN d ON l.parent_id=d.id) "
                "SELECT 1 FROM tasks JOIN d ON tasks.id=d.id WHERE status='running' LIMIT 1", (task_id,)).fetchone():
            return {"applied": False, "reason": "descendant_active"}
        status = "done" if action == "close" else kb._landing_status_after_parents(conn, task_id)
        conn.execute("UPDATE tasks SET status=?,completed_at=CASE WHEN ?='done' THEN ? ELSE NULL END,"
                     "result=?,block_kind=NULL,last_failure_error=NULL WHERE id=?",
                     (status, status, int(kb.time.time()), reason, task_id))
        if action == "reopen":
            kb.invalidate_descendants_for_parent_reopen(conn, task_id, author="measurement")
            kb.add_comment(conn, task_id, author="measurement", body=reason +
                           "\nLeg over deze heropening of terugdraai één les vast via je bestaande ACE-leerroute; "
                           "onderzoek en herstel op dezezelfde kaart. Geen apart verwerkingsbericht.")
        payload = {"action": action, "expected_run_id": expected_run_id, "profile": profile,
                   "evidence_ref": evidence_ref, "evidence_refs": evidence_refs, "reason": reason,
                   "status": status, "learning_required": action == "reopen"}
        kb._append_event(conn, task_id, "measured_outcome", payload)
        if action == "close" and task.status != "done":
            kb._append_event(conn, task_id, "completed", {"summary": reason, "metadata": payload})
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='measured_outcome' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    kb.recompute_ready(conn)
    return {"applied": True, "replay": False, "event_id": event["id"], "status": status}


def main(argv=None):
    from hermes_cli import kanban_db as kb
    args = sys.argv[1:] if argv is None else argv
    db_path, payload = args
    with kb.connect(db_path) as conn:
        result = apply_measured_outcome(conn, **json.loads(payload))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
