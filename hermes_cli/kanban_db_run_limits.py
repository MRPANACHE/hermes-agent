"""Native admission limits and source-bound Mara dispositions; no second scheduler."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import json
from zoneinfo import ZoneInfo

DECISION_SCHEMA = "mrpanache.run-limit-decision.v1"


def day_window(now: int, timezone: str = "Europe/Amsterdam") -> dict:
    zone = ZoneInfo(timezone)
    day = datetime.fromtimestamp(now, zone).date()
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return {"timezone": timezone, "local_date": day.isoformat(),
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "start": int(start.timestamp()), "end": int(end.timestamp())}


def execution_runs(conn, *, task_id=None, after_event_id=0):
    """Only real worker starts spend attempts or hours (one row per run)."""
    where = "AND r.task_id=?" if task_id is not None else ""
    return conn.execute(
        "SELECT r.*, MIN(e.id) AS spawn_event_id FROM task_runs r "
        "JOIN task_events e ON e.task_id=r.task_id AND e.run_id=r.id AND e.kind='spawned' "
        "WHERE e.id>? " + where + " GROUP BY r.id ORDER BY r.id",
        (after_event_id, task_id) if task_id is not None else (after_event_id,),
    ).fetchall()


def _runtime_seconds(conn, now, window):
    return sum(max(0, min(now, window["end"], row["ended_at"] if row["ended_at"] is not None else now)
                   - max(window["start"], row["started_at"])) for row in execution_runs(conn))


def daily_usage(conn, now: int) -> dict:
    """Fleet total over the native shared-board registry, not the caller's pin.

    Open other boards read-only: a worker's HERMES_KANBAN_DB/current-board
    binding must neither hide those boards nor cause the same DB to count twice.
    Retained archived boards still spent runtime today. Unreadable accounting
    raises before admission rather than silently granting an incomplete budget.
    """
    import os
    import sqlite3
    from pathlib import Path
    from hermes_cli import kanban_db as kb

    window = day_window(now)
    own = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    seen = set()
    if own:
        info = Path(own).stat()
        seen.add((info.st_dev, info.st_ino))
    seconds = _runtime_seconds(conn, now, window)
    accounted = [own or ":memory:"]
    paths = [kb.kanban_home() / "kanban.db"]
    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pin:
        paths.append(Path(pin).expanduser())
    paths.extend(kb.boards_root() / entry["slug"] / "kanban.db"
                 for entry in kb.list_boards(include_archived=True) if entry["slug"] != kb.DEFAULT_BOARD)
    archive = kb.boards_root() / "_archived"
    if archive.exists():
        paths.extend(sorted(archive.glob("*/kanban.db")))
    for path in paths:
        if not path.exists():
            continue
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        other = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        other.row_factory = sqlite3.Row
        try:
            seconds += _runtime_seconds(other, now, window)
            accounted.append(str(path.resolve()))
        finally:
            other.close()
    return {**window, "runtime_seconds": seconds, "hours": seconds / 3600,
            "accounting_scope": "shared_boards", "accounted_boards": accounted}


def _payload(raw):
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_supervisor_decision(conn, task_id, decision, *, before_event_id=None):
    """Validate data only; the existing OOS delivery route owns authentication.

    No title inference, implicit reset or generic bypass. An explicit decision is
    bound to this native origin and the exact actual execution prefix it assessed.
    Mara owns whether a linked case's measurement constitutes a series outcome.
    """
    def require(ok):
        if not ok:
            raise ValueError("run_limit_decision_binding_invalid")
    require(isinstance(decision, dict))
    common = {"schema", "task_id", "origin_event_id", "run_ids", "kind", "reason", "evidence_refs"}
    extras = {"classification": {"work_class"}, "measured_closure": {"case_ref", "measurement_ref"},
              "new_series": {"limit_event_id"}}
    kind = decision.get("kind")
    require(isinstance(kind, str) and kind in extras)
    require(set(decision) == common | extras[kind] and decision.get("schema") == DECISION_SCHEMA
            and decision.get("task_id") == task_id)
    origin = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1", (task_id,)).fetchone()
    require(origin is not None and type(decision["origin_event_id"]) is int and decision["origin_event_id"] == origin["id"])
    runs = [r["id"] for r in execution_runs(conn, task_id=task_id)
            if before_event_id is None or r["spawn_event_id"] < before_event_id]
    require(isinstance(decision["run_ids"], list) and all(type(x) is int for x in decision["run_ids"])
            and decision["run_ids"] == runs)
    require(isinstance(decision["reason"], str) and 0 < len(decision["reason"].strip()) <= 1000)
    refs = decision["evidence_refs"]
    require(isinstance(refs, list) and 0 < len(refs) <= 20 and
            all(isinstance(x, str) and 0 < len(x) <= 500 and x.startswith(("oos://", "hermes://")) for x in refs))
    if kind == "classification":
        require(decision["work_class"] in ("regular_work", "incident_work", "noor_day_work"))
        if decision["work_class"] == "noor_day_work":
            require(conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "noor")
    elif kind == "measured_closure":
        require(bool(runs) and all(isinstance(decision[k], str) and decision[k].startswith("oos://")
                                  and decision[k] in refs for k in ("case_ref", "measurement_ref")))
        require(decision["case_ref"] != decision["measurement_ref"])
    else:
        require(type(decision["limit_event_id"]) is int)
        hit = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND id=? AND kind='limit_hit'",
                           (task_id, decision["limit_event_id"])).fetchone()
        require(hit is not None and (before_event_id is None or hit["id"] < before_event_id))
        p = _payload(hit["payload"])
        require(p.get("limit_key") == "task_runs_without_outcome" and p.get("enforced") is True
                and p.get("action") == "blocked_new_run" and not p.get("assessment_used")
                and p.get("run_ids") and p["run_ids"] == runs[-len(p["run_ids"]):])
    return decision


def _oos_supervisor_decisions(conn, task_id):
    """Only consume grant-checked OOS decisions, not comments/actor strings alone."""
    for row in conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind IN "
                            "('mrpanache_clarified','mrpanache_resumed') ORDER BY id", (task_id,)):
        p = _payload(row["payload"])
        answer = p.get("answer")
        delivery = p.get("oos_delivery")
        if not isinstance(answer, dict) or not isinstance(delivery, dict):
            continue
        d = answer.get("run_limit_decision")
        if not (d and p.get("actor_requested_by") == "agent:mara" and p.get("actor_via") == "oos-decisions"
                and p.get("actor_origin_task_id") == task_id and delivery.get("answered_by") == "mara"
                and isinstance(delivery.get("delivered_by"), str) and delivery["delivered_by"]):
            continue
        # Require the native delivery's exact receipt too: a copied answer payload
        # without its canonical delivery transaction cannot grant an exception.
        receipt = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='oos_decision_delivery' "
                               "AND json_extract(payload,'$.request_id')=?", (task_id, p.get("request_id"))).fetchone()
        receipt = _payload(receipt["payload"]).get("receipt", {}) if receipt else {}
        if not isinstance(receipt, dict) or not isinstance(receipt.get("native_receipt"), dict):
            continue
        if (receipt.get("decision_id") != delivery.get("decision_id") or
                receipt.get("revision_sha256") != delivery.get("revision_sha256") or
                receipt.get("answered_by") != "mara" or receipt.get("delivered_by") != delivery["delivered_by"] or
                receipt["native_receipt"].get("event_id") != row["id"]):
            continue
        try:
            validate_supervisor_decision(conn, task_id, d, before_event_id=row["id"])
        except ValueError:
            continue
        yield row["id"], d


def supervisor_decisions(conn, task_id):
    """Two existing authority surfaces, one typed policy: OOS or native Mara.

    Plain comments/claimed actor strings do not grant authority. The native
    kanban_comment handler stamps a separate event only after checking its
    persisted runtime identity; require its exact comment row and assessment.
    This also covers coordinator-created cards with no MCP ingress metadata.
    """
    decisions = list(_oos_supervisor_decisions(conn, task_id))
    rows = conn.execute(
        "SELECT e.id,e.payload,c.body,c.author FROM task_events e "
        "JOIN task_comments c ON c.task_id=e.task_id "
        "AND c.id=json_extract(e.payload,'$.comment_id') "
        "WHERE e.task_id=? AND e.kind='run_limit_decision' ORDER BY e.id", (task_id,))
    for row in rows:
        payload = _payload(row["payload"])
        decision = payload.get("decision")
        if (row["author"] != "mara" or payload.get("author") != "mara"
                or payload.get("via") != "kanban_comment" or _payload(row["body"]) != decision):
            continue
        try:
            validate_supervisor_decision(conn, task_id, decision, before_event_id=row["id"])
        except ValueError:
            continue
        decisions.append((row["id"], decision))
    yield from sorted(decisions, key=lambda item: item[0])


def task_series(conn, task_id: str) -> dict:
    frontier = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0]
    for event_id, d in supervisor_decisions(conn, task_id):
        if d["kind"] == "measured_closure":
            frontier = max(frontier, event_id)
    rows = execution_runs(conn, task_id=task_id, after_event_id=frontier)
    return {"outcome_event_id": frontier, "run_ids": [r["id"] for r in rows]}


def _single_assessment(conn, task_id, series):
    for event_id, d in supervisor_decisions(conn, task_id):
        if event_id <= series["outcome_event_id"] or d["kind"] != "new_series":
            continue
        hit = conn.execute("SELECT payload FROM task_events WHERE id=?", (d["limit_event_id"],)).fetchone()
        p = _payload(hit["payload"])
        if p.get("outcome_event_id") == series["outcome_event_id"]:
            return len(p["run_ids"])
    return None


def _exception(conn, task_id):
    result = None
    for event_id, decision in supervisor_decisions(conn, task_id):
        if decision["kind"] == "classification":
            result = {"event_id": event_id, **decision}
    return result if result and result["work_class"] != "regular_work" else None


def _record_hit(conn, task_id, payload):
    from hermes_cli.kanban_db import _append_event
    existing = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='limit_hit'", (task_id,)).fetchall()
    if not any(_payload(r["payload"]).get("episode_key") == payload["episode_key"] for r in existing):
        _append_event(conn, task_id, "limit_hit", payload)


def enforce_run_limits(conn, task_id: str, source_status: str, now: int) -> bool:
    """In the native ready/review claim transaction; never cancel running workers."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.kanban_db import _append_event
    if (load_config_readonly() or {}).get("kanban", {}).get("hard_run_limits", False) is not True:
        return True
    task = conn.execute("SELECT status,claim_lock,worker_pid,current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (not task or task["status"] != source_status or task["claim_lock"] is not None
            or task["worker_pid"] is not None or task["current_run_id"] is not None):
        return False
    series = task_series(conn, task_id)
    base = _single_assessment(conn, task_id, series)
    hits = []
    if len(series["run_ids"]) >= (base + 5 if base is not None else 5):
        hits.append({"limit_key": "task_runs_without_outcome", "observed": len(series["run_ids"]),
                     "threshold": 5, "unit": "runs", "timezone": "Europe/Amsterdam",
                     "run_ids": series["run_ids"], "outcome_event_id": series["outcome_event_id"],
                     "assessment_used": base is not None,
                     "episode_key": f"task:{task_id}:{series['outcome_event_id']}:{int(base is not None)}"})
    usage = daily_usage(conn, now)
    if usage["runtime_seconds"] >= 20 * 3600:
        exception = _exception(conn, task_id)
        daily = {"limit_key": "daily_run_hours", "observed": usage["hours"], "threshold": 20, "unit": "hours",
                 **{k: usage[k] for k in ("timezone", "local_date", "window_start", "window_end")},
                 "accounting_scope": usage["accounting_scope"], "board_count": len(usage["accounted_boards"]),
                 "episode_key": f"day:{usage['local_date']}:{task_id}:" + str(exception["event_id"] if exception else 0)}
        if exception and not hits:
            _record_hit(conn, task_id, {**daily, "action": "exception_allowed", "enforced": True,
                        "exception_reason": exception["reason"], "exception_class": exception["work_class"],
                        "exception_event_id": exception["event_id"], "evidence_refs": exception["evidence_refs"]})
        else:
            hits.append(daily)
    for hit in hits:
        _record_hit(conn, task_id, {**hit, "action": "blocked_new_run", "enforced": True})
    if not hits:
        return True
    # Day exhaustion is not a card fault: retain ready/review for next midnight.
    if hits[0]["limit_key"] == "task_runs_without_outcome":
        conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input',last_failure_error=? WHERE id=?",
                     ("Mara: beoordeel runlimiet; hooguit één nieuwe reeks", task_id))
        _append_event(conn, task_id, "blocked", {"reason": "run_limit:task_runs_without_outcome", "kind": "needs_input",
                                               "source_status": source_status, "assessment_used": base is not None})
    return False
