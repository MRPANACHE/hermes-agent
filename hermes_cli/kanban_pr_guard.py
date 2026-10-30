"""Bounded, profile-owned PR reads and durable ready-lane guard episodes.

A URL is a risk signal, not proof of an open PR. Network reads never hold the
board write lock; a terminal-state receipt is bound again at atomic claim.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time

PR_ESCALATION_SECONDS = 900
PR_EVIDENCE_SECONDS = 15
PR_LOOKUP_SECONDS = 3
PR_LOOKUP_LIMIT = 4


def snapshot(conn, task_id):
    """Bind local authority and all PR sources, including linked parent state."""
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    comments = [dict(r) for r in conn.execute(
        "SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (task_id,))]
    frontier = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=? "
        "AND kind NOT IN ('heartbeat','respawn_guarded')", (task_id,)).fetchone()[0]
    parents = [dict(r) for r in conn.execute(
        "SELECT p.* FROM task_links l LEFT JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY l.parent_id", (task_id,))]
    value = {"task": dict(task) if task else None, "comments": comments,
             "frontier": frontier, "parents": parents}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()



def pr_comments(conn, task_id, now):
    """Recent PR sources plus a pending episode; age never proves terminal state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_WINDOW, _rows_since
    rows = list(_rows_since(conn, "task_comments", task_id, now - _RESPAWN_GUARD_PR_WINDOW,
                            cols=", body, author, id", unreadable=now))
    last = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='respawn_guarded' "
        "AND json_valid(payload) AND json_extract(payload,'$.episode') IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not last:
        return rows
    prior = kb._json_dict(last["payload"])
    task = kb.get_task(conn, task_id)
    if not task or prior.get("owner") != task.assignee or prior.get("frontier") != episode_frontier(conn, task_id):
        return rows
    known = {c["id"] for _, c in rows}
    ids = {ref["comment_id"] for refs in prior.get("sources", {}).values() for ref in refs} - known
    for cid in sorted(ids):
        row = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND id=?", (task_id, cid)).fetchone()
        if row:
            rows.append((kb._to_epoch(row["created_at"]) or now, row))
    return rows


def episode_frontier(conn, task_id):
    return conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=? "
        "AND kind NOT IN ('commented','heartbeat','respawn_guarded')", (task_id,)).fetchone()[0]


def pr_sources(conn, task_id):
    """Retain each URL's parent delivery provenance without parsing prose intent.

    Parent delivery is an exact metadata binding, not permission to resume against
    an open/unknown own PR. Routine comments cannot erase that native binding.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    deliveries = {}
    for r in conn.execute(
        "SELECT p.id AS parent_task_id,r.id AS parent_run_id,p.assignee,"
        "r.profile,r.outcome,r.ended_at,r.metadata "
        "FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "JOIN task_runs r ON r.id=(SELECT MAX(id) FROM task_runs WHERE task_id=p.id) "
        "WHERE l.child_id=? AND p.status IN ('done','archived')", (task_id,)):
        url = kb._json_dict(r["metadata"]).get("pr_url")
        if (isinstance(url, str) and r["profile"] == r["assignee"]
                and r["outcome"] == "completed" and r["ended_at"] is not None):
            deliveries[url] = (r["parent_task_id"], r["assignee"],
                               kb._to_epoch(r["ended_at"]), r["parent_run_id"])
    sources = {}
    now = int(time.time())
    for stamp, c in pr_comments(conn, task_id, now):
        for url in _RESPAWN_GUARD_PR_URL_RE.findall(kb._lossy_text(c["body"]) or ""):
            parent = deliveries.get(url)
            provenance = {"comment_id": c["id"], "parent_task_id": None, "parent_run_id": None}
            if parent and parent[1] == c["author"] and parent[2] is not None and stamp <= parent[2]:
                provenance["parent_task_id"] = parent[0]
                provenance["parent_run_id"] = parent[3]
            sources.setdefault(url, []).append(provenance)
    return sources


def collect_pr_evidence(conn, task_id):
    """At most four three-second reads; unknown, denied and malformed fail closed."""
    from hermes_cli.kanban_pr_acceptance import _PR, _assignee_profile_home, _gh_env, _GateAuthError
    before = snapshot(conn, task_id)
    sources = pr_sources(conn, task_id)
    row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    evidence = {"snapshot": before, "observed_at": time.time(), "states": {}, "sources": sources}
    if row is None:
        return evidence
    try:
        env = _gh_env(_assignee_profile_home(row["assignee"]))
    except (_GateAuthError, OSError, ValueError, RuntimeError):
        return evidence
    for url in sorted(sources)[:PR_LOOKUP_LIMIT]:
        match = _PR.fullmatch(url)
        if not match:
            continue
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{match[1]}/pulls/{match[2]}", "--hostname", "github.com"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=PR_LOOKUP_SECONDS, check=True, env=env)
            pr = json.loads(result.stdout)
            # Validate identity as well as state; partial/error responses never unlock.
            if pr["html_url"] != url or type(pr.get("merged")) is not bool:
                continue
            if pr["state"] == "closed":
                evidence["states"][url] = "MERGED" if pr["merged"] else "CLOSED"
            elif pr["state"] == "open" and not pr["merged"]:
                evidence["states"][url] = "OPEN"
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            # Never store stderr or secrets in board diagnostics.
            continue
    return evidence


def evidence_current(conn, task_id, evidence):
    age = time.time() - evidence["observed_at"]
    return 0 <= age <= PR_EVIDENCE_SECONDS and evidence["snapshot"] == snapshot(conn, task_id)


def record_pr_guard(conn, task_id, evidence):
    """One initial event and one owner escalation at 15m per continuous episode.

    The task stays ready, so a terminal PR read on a later tick can still claim.
    This is board/journal visibility only, never an unsolicited operator message.
    """
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if not task or task.status != "ready" or not evidence_current(conn, task_id, evidence):
            return
        frontier = episode_frontier(conn, task_id)
        urls = sorted(evidence["sources"])
        key = hashlib.sha256(json.dumps([frontier, task.assignee, urls]).encode()).hexdigest()
        first = conn.execute(
            "SELECT created_at,payload FROM task_events WHERE task_id=? AND kind='respawn_guarded' "
            "AND json_valid(payload) AND json_extract(payload,'$.episode')=? ORDER BY id LIMIT 1",
            (task_id, key)).fetchone()
        elapsed = int(time.time()) - first["created_at"] if first else 0
        escalated = bool(first and elapsed >= PR_ESCALATION_SECONDS)
        if first and (not escalated or conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='respawn_guarded' "
            "AND json_valid(payload) AND json_extract(payload,'$.episode')=? "
            "AND json_extract(payload,'$.escalated')=1 LIMIT 1", (task_id, key)).fetchone()):
            return
        payload = {"reason": "active_pr", "episode": key, "owner": task.assignee, "frontier": frontier,
                   "escalated": escalated, "elapsed_seconds": elapsed,
                   "threshold_seconds": PR_ESCALATION_SECONDS,
                   "states": {u: evidence["states"].get(u, "UNKNOWN") for u in urls},
                   "sources": evidence["sources"],
                   "next_action": "Owner: inspect PR state/access or use the existing explicit review/handoff route."}
        kb._append_event(conn, task_id, "respawn_guarded", payload)
        if escalated:
            kb._log.warning("kanban PR guard unresolved for %ss: task=%s owner=%s; inspect native guard evidence",
                            elapsed, task_id, task.assignee)


def resolve_guard(conn, task_id, *, lane):
    """Resolve only an active-PR guard; stronger local guards never cause I/O."""
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    reason = check_respawn_guard(conn, task_id, lane=lane)
    evidence = None
    if reason == "active_pr":
        evidence = collect_pr_evidence(conn, task_id)
        if evidence_current(conn, task_id, evidence):
            reason = check_respawn_guard(conn, task_id, lane=lane, pr_states=evidence["states"])
    return reason, evidence


def record_guard(conn, task_id, reason, evidence):
    from hermes_cli import kanban_db as kb
    if reason == "active_pr" and evidence is not None:
        record_pr_guard(conn, task_id, evidence)
    else:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "respawn_guarded", {"reason": reason})
