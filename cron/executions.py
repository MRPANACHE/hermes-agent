"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their owner process is proved gone — a start-time reading that fails
to match the claim-time fingerprint is not proof of death. Terminal states are immutable.
"""

from __future__ import annotations

import logging
import math
import os
import json
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM
from hermes_cli.observability.shared_metrics_gateway import record_cron_finish

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
# Floor for the live-owner stale-claim bound (#115692); see _live_owner_stale_after_seconds.
LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS = 7200.0
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
_store_override: ContextVar[Optional[Path]] = ContextVar("cron_execution_store", default=None)


# --- executions ledger --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = _store_override.get() or EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
    return open_db(path, db_label="cron/executions.db", synchronous_full=True, initialize=_initialize_schema)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    add_column_if_missing(conn, "executions", "progress_at", "progress_at TEXT")
    for name in ("session_id", "output_path", "recovery_expires_at", "recovery_scope_hash"):
        add_column_if_missing(conn, "executions", name, f"{name} TEXT")
    # Recovery bindings survive ordinary execution pruning and job deletion.
    conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_recoveries (
        failure_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
        binding TEXT NOT NULL, extra_prompt TEXT NOT NULL,
        admitted_at TEXT NOT NULL, phase TEXT NOT NULL,
        execution_id TEXT, reason TEXT)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


@contextmanager
def use_execution_store(home):
    """Storage-only ingress scope; never changes the executing profile."""
    token = _store_override.set(Path(home).resolve() / "cron" / "executions.db")
    try:
        yield
    finally:
        _store_override.reset(token)


def _connect_readonly() -> Optional[sqlite3.Connection]:
    """Open the execution store query-only, without creating or migrating it."""
    path = _store_override.get() or EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def recovery_job_scope(job):
    from cron.jobs import _apply_skill_fields
    job = _apply_skill_fields(job)
    mutable = {"fire_claim", "run_claim", "last_run_at", "last_status", "last_error",
               "last_delivery_error", "failure_streak", "next_run_at", "execution_id",
               "last_failure", "last_fire_error", "last_dispatch", "pending_slot",
               "_scheduled_instant", "unreachable_retry", "quota_hold_until",
               "quota_hold_cron_expr", "preflight_alerted", "last_delivery_queued",
               "_preserve_next_run_at", "_scheduled_recovery_prompt"}
    scope = {k: v for k, v in job.items() if k not in mutable}
    if isinstance(scope.get("repeat"), dict):
        scope["repeat"] = {k: v for k, v in scope["repeat"].items() if k != "completed"}
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def recovery_lineage_snapshot(conn: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    """Return the canonical native blocked-request lineage for recovery.

    This is intentionally shared by ingress and the owner tick.  It only reads
    the caller's already-open native board connection; recovery events are not
    part of the mandate hash.
    """
    conn.row_factory = sqlite3.Row

    def deny(reason: str) -> None:
        raise ValueError(reason)

    task = conn.execute(
        "SELECT id,body,assignee,status,current_run_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if (not task or task["assignee"] != "otto" or task["status"] != "blocked"
            or task["current_run_id"] is not None):
        deny("recovery_task_not_blocked")
    body = task["body"] or ""
    try:
        metadata = json.loads(body.split("\n", 1)[0])
    except (TypeError, ValueError):
        deny("recovery_request_provenance_missing")
    if (not isinstance(metadata, dict)
            or metadata.get("schema") != "mrpanache.agent-request.v1"
            or metadata.get("agent") != "otto"
            or metadata.get("kind") not in {"codework", "operation"}):
        deny("recovery_request_provenance_invalid")

    rows = conn.execute(
        "SELECT id,kind,run_id,payload FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    transition_kinds = {
        "mrpanache_resumed", "mrpanache_recovered", "mrpanache_reworked",
        "mrpanache_clarified",
    }
    mandate_events = []
    effective_goal = metadata.get("delivery_goal")
    for row in rows:
        if row["kind"] not in transition_kinds:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            deny("recovery_event_invalid")
        if not isinstance(payload, dict):
            deny("recovery_event_invalid")
        mandate_events.append({"id": row["id"], "kind": row["kind"], "payload": payload})
        if "delivery_goal" in payload:
            effective_goal = payload["delivery_goal"]
        elif isinstance(payload.get("meta"), dict) and "delivery_goal" in payload["meta"]:
            effective_goal = payload["meta"]["delivery_goal"]
    if effective_goal != "verified_live":
        deny("recovery_delivery_goal_not_verified_live")

    run = conn.execute(
        "SELECT id,outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    block = next((row for row in reversed(rows) if row["kind"] == "blocked"), None)
    if (not run or run["outcome"] != "blocked"
            or block is None or block["run_id"] != run["id"]):
        deny("recovery_terminal_lineage_invalid")
    fingerprint = metadata.get("fingerprint") or metadata.get("task_fingerprint")
    conversation_id = metadata.get("conversation_id")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or not isinstance(conversation_id, str) or not conversation_id):
        deny("recovery_request_metadata_invalid")
    mandate_hash = hashlib.sha256(json.dumps(
        {"body": body, "mandate_events": mandate_events},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return {
        "task_id": task_id, "requester": "otto", "run_id": run["id"],
        "block_event_id": block["id"], "task_fingerprint": fingerprint,
        "conversation_id": conversation_id, "mandate_hash": mandate_hash,
    }


def recovery_failure_expiry(failure, job):
    """Use only occurrence evidence written at native dispatch, never live cadence.

    Legacy/manual attempts without this evidence remain unknown. In particular,
    claimed_at, session names and today's schedule cannot reconstruct an occurrence.
    """
    from cron import jobs
    expires = failure.get('recovery_expires_at')
    scope = failure.get('recovery_scope_hash')
    if not expires or not scope:
        raise ValueError('hermes_job_scheduled_occurrence_boundary_unknown')
    if scope != recovery_job_scope(job):
        raise ValueError('hermes_job_scheduled_recovery_binding_stale')
    try:
        boundary = jobs.datetime.fromisoformat(expires)
        if boundary.tzinfo is None:
            raise ValueError('missing timezone')
    except (TypeError, ValueError) as exc:
        raise ValueError('hermes_job_scheduled_occurrence_boundary_unknown') from exc
    if _hermes_now() >= boundary:
        raise ValueError('hermes_job_stale_scheduled_failure')
    return expires


def _recovery_failure(conn, binding):
    row = conn.execute("SELECT * FROM executions WHERE job_id=? "
                       "ORDER BY julianday(claimed_at) DESC,claimed_at DESC,id DESC LIMIT 1", (binding["job_id"],)).fetchone()
    # ``session_id`` was added after the first ledger format.  A missing
    # legacy value is deliberately treated as NULL; it is never guessed or
    # backfilled from a later scheduler session.
    session_id = row["session_id"] if row is not None and "session_id" in row.keys() else None
    if not row or row["id"] != binding["failure_id"] or row["status"] != "failed" or session_id != binding.get("failed_session_id"):
        raise ValueError("hermes_job_stale_scheduled_failure")
    expires = row['recovery_expires_at'] if 'recovery_expires_at' in row.keys() else None
    scope = row['recovery_scope_hash'] if 'recovery_scope_hash' in row.keys() else None
    from cron import jobs
    if (not expires or expires != binding.get('failure_expires_at')
            or scope != binding['job_scope_hash']
            or _hermes_now() >= jobs.datetime.fromisoformat(expires)):
        raise ValueError('hermes_job_stale_scheduled_failure')
    if conn.execute("SELECT 1 FROM executions WHERE job_id=? AND status IN ('claimed','running')",
                    (binding["job_id"],)).fetchone():
        raise ValueError("hermes_job_scheduled_execution_active")


def admit_scheduled_recovery(binding, extra_prompt):
    """Called only by trusted ingress after owner/job/lineage checks.

    The failure, not a caller's request ID, is the durable dedup key. Replaying
    a different binding never creates a second run, even after attempt pruning.
    """
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM scheduled_recoveries WHERE failure_id=?",
                           (binding["failure_id"],)).fetchone()
        if old:
            if old["binding"] != encoded or old["extra_prompt"] != extra_prompt:
                raise ValueError("hermes_job_idempotency_conflict")
            return dict(old)
        _recovery_failure(conn, binding)
        conn.execute("INSERT INTO scheduled_recoveries VALUES (?,?,?,?,?,'admitted',NULL,NULL)",
                     (binding["failure_id"], binding["job_id"], encoded, extra_prompt,
                      _hermes_now().isoformat()))
        return dict(conn.execute("SELECT * FROM scheduled_recoveries WHERE failure_id=?",
                                 (binding["failure_id"],)).fetchone())


def scheduled_recovery_status(failure_id):
    """Read one existing recovery ledger row without creating/migrating it."""
    conn = _connect_readonly()
    if conn is None:
        return None
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_recoveries'").fetchone() is None:
            return None
        row = conn.execute("SELECT * FROM scheduled_recoveries WHERE failure_id=?", (failure_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        execution_id = row["execution_id"]
        execution = None
        if execution_id:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'").fetchone() is not None:
                execution = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        result["execution"] = dict(execution) if execution else None
        return result
    finally:
        conn.close()


def latest_scheduled_failure(job_id: str) -> Optional[Dict[str, Any]]:
    """Read the newest failed attempt, query-only and without schema init."""
    conn = _connect_readonly()
    if conn is None:
        return None
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'").fetchone() is None:
            return None
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(executions)")}
        wanted = ["id", "job_id", "source", "status", "claimed_at", "started_at",
                  "finished_at", "error", "session_id", "output_path",
                  "recovery_expires_at", "recovery_scope_hash"]
        select = ", ".join(name if name in columns else f"NULL AS {name}" for name in wanted)
        row = conn.execute(
            f"SELECT {select} FROM executions WHERE job_id=? "
            "ORDER BY julianday(claimed_at) DESC,claimed_at DESC,id DESC LIMIT 1", (str(job_id),)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def scheduled_recovery_by_ref(ref: str) -> Optional[Dict[str, Any]]:
    """Find a recovery by its canonical SHA-256 binding reference.

    The reference is recomputed from the decoded JSON, rather than trusting a
    stored/queried auxiliary field.  This remains a read-only operation and
    returns the ledger row together with its actual execution, if retained.
    """
    if not isinstance(ref, str) or len(ref) != 64:
        return None
    conn = _connect_readonly()
    if conn is None:
        return None
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_recoveries'").fetchone() is None:
            return None
        rows = conn.execute("SELECT * FROM scheduled_recoveries").fetchall()
        for row in rows:
            try:
                binding = json.loads(row["binding"])
                encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != ref:
                continue
            result = dict(row)
            execution = None
            if row["execution_id"] and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'").fetchone() is not None:
                execution = conn.execute("SELECT * FROM executions WHERE id=?", (row["execution_id"],)).fetchone()
            result["execution"] = dict(execution) if execution else None
            return result
        return None
    finally:
        conn.close()


def _recovery_lineage(binding):
    # Board identity is the existing native default board, not a supplied path.
    from hermes_cli.kanban_db import kanban_db_path
    lineage = binding["lineage"]
    path = kanban_db_path(board="default")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        try:
            current = recovery_lineage_snapshot(conn, lineage["task_id"])
        except ValueError as exc:
            raise ValueError("hermes_job_scheduled_lineage_changed") from exc
        if current != lineage:
            raise ValueError("hermes_job_scheduled_lineage_changed")


def claim_scheduled_recovery(failure_id):
    """Owner-tick only. Fence → strict jobs lock → ledger CAS → native claim.

    Write possibly_fired BEFORE the separate jobs claim. A crash across the
    two stores is unknown and never replayed. Before this CAS an admission is
    restart-safe. No claim of a joint JSON/SQLite/board transaction is made.
    """
    from cron import jobs
    record = scheduled_recovery_status(failure_id)
    if not record or record["phase"] != "admitted":
        return None
    binding = json.loads(record["binding"])
    home = str(get_hermes_home().resolve())
    if home != binding["owner_home"] or jobs._current_cron_store().cron_dir != Path(home) / "cron":
        raise ValueError("hermes_job_scheduled_owner_mismatch")
    with jobs._fire_job_lock(binding["job_id"]) as fenced:
        if not fenced:
            raise ValueError("hermes_job_scheduled_lock_unavailable")
        with jobs._jobs_lock(strict=True):
            job = next((j for j in jobs.load_jobs() if j["id"] == binding["job_id"]), None)
            if not job or recovery_job_scope(job) != binding["job_scope_hash"]:
                raise ValueError("hermes_job_scheduled_scope_changed")
            if job.get("fire_claim") or not jobs.is_job_runnable(job):
                raise ValueError("hermes_job_scheduled_execution_active")
            _recovery_lineage(binding)
            # Never consume an old missed assessment at/after the next cadence.
            if job.get("next_run_at") != binding["next_run_at"] or _hermes_now() >= jobs._ensure_aware(jobs.datetime.fromisoformat(binding["next_run_at"])):
                raise ValueError("hermes_job_stale_scheduled_failure")
            with _transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT phase FROM scheduled_recoveries WHERE failure_id=?",
                                       (failure_id,)).fetchone()
                if not current or current[0] != "admitted":
                    return None
                _recovery_failure(conn, binding)
                conn.execute("UPDATE scheduled_recoveries SET phase='possibly_fired' WHERE failure_id=?",
                             (failure_id,))
            # Native fire fences are reentrant; a recovery is outside the cadence and
            # must not stamp the upcoming occurrence completed or advance its schedule.
            claimed = jobs.claim_job_for_fire(
                binding["job_id"], return_job=True, manual=True, advance_schedule=False
            )
            if not isinstance(claimed, dict):
                return None  # tombstone remains ambiguous; never retry
            execution = create_execution(binding["job_id"], source="scheduled_recovery")
            with _transaction() as conn:
                conn.execute("UPDATE scheduled_recoveries SET execution_id=? WHERE failure_id=?",
                             (execution["id"], failure_id))
            claimed["execution_id"] = execution["id"]
            return claimed, record["extra_prompt"]


def admitted_scheduled_recoveries():
    """Read pending admissions without creating or migrating an idle profile store."""
    conn = _connect_readonly()
    if conn is None:
        return []
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_recoveries'").fetchone() is None:
            return []
        return [r[0] for r in conn.execute("SELECT failure_id FROM scheduled_recoveries WHERE phase='admitted'")]
    finally:
        conn.close()


def record_execution_evidence(execution_id, *, session_id=None, output_path=None):
    """Bind only actual native executor evidence, never assessment/PB success."""
    with _transaction() as conn:
        conn.execute('UPDATE executions SET session_id=COALESCE(?,session_id), '
                     'output_path=COALESCE(?,output_path) WHERE id=?',
                     (session_id, str(output_path) if output_path is not None else None, execution_id))


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    if current is None:
        return True  # cannot compare -> cannot prove death; a misread must not rewrite state
    # Drifted same-host readings (#117505) are not proof of death; a live misread is still
    # bounded by the stale-claim sweep below.
    from gateway.status import start_time_fingerprints_match
    return start_time_fingerprints_match(started_at, current)


def _live_owner_stale_after_seconds() -> Optional[float]:
    """Age past which a claimed/running row with a LIVE owner is treated as wedged.

    Derived from the existing knobs, never a bare wall-clock constant:
    ``max(3 × HERMES_CRON_TIMEOUT, cron script timeout, 7200)``. Returns ``None`` (never reclaim
    live owners — today's behaviour) when the inactivity timeout is 0/unlimited or not a finite
    positive number: with no bound to derive from, fail closed.
    """
    from cron.scheduler import _cron_inactivity_seconds
    from cron.scheduler_script import _get_script_timeout

    inactivity = float(_cron_inactivity_seconds())
    if not math.isfinite(inactivity) or inactivity <= 0:
        return None
    return max(
        inactivity * CLAIM_TTL_INACTIVITY_HEADROOM,
        float(_get_script_timeout()),
        LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS,
    )


def _claim_age_seconds(claimed_at: str) -> float:
    """Seconds since ``claimed_at`` (NOT NULL, always the aware ISO string from hermes_time.now)."""
    return (_hermes_now() - datetime.fromisoformat(claimed_at)).total_seconds()


def _stale_age_seconds(claimed_at: str, progress_at: Optional[str]) -> float:
    """Seconds since the owner last proved it was making progress.

    A wedged worker (#115692) stops stamping ``progress_at``; a long but healthy run keeps
    stamping it, so the derived stale bound measures silence, not run length. Rows written
    before the column existed have no stamp and fall back to claim age.
    """
    return _claim_age_seconds(progress_at or claimed_at)


def touch_execution_progress(execution_id: str) -> bool:
    """Stamp ``progress_at`` on a running attempt this process owns. Returns False when the row
    is no longer ours or no longer running (the caller treats that as informational only)."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET progress_at=?
               WHERE id=? AND status IN ('claimed','running') AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        return cur.rowcount == 1


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY julianday(finished_at) DESC, finished_at DESC,
                      julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (max(0, int(MAX_TERMINAL_EXECUTIONS)),),
    )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
    recovery_expires_at: Optional[str] = None, recovery_scope_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant, recovery_expires_at, recovery_scope_hash)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, canonical_instant(scheduled_instant),
             recovery_expires_at, recovery_scope_hash),
        )
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    with _transaction() as conn:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (scheduled_instant(instant), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")


def mark_execution_handoff_pending(execution_id: str) -> Optional[Dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, delivery_outcome, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _fetch(conn, execution_id)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    record_cron_finish(record, delivery_outcome)
    return record


_OWNER_GONE_REASON = (
    "Scheduler restarted after this execution's owner exited before a durable "
    "terminal state; whether side effects ran is unknown."
)
_OWNER_WEDGED_REASON = (
    "Owner process is still alive but the claim outlived the derived stale bound; "
    "treated as wedged (#115692). The process was not terminated; whether side effects "
    "ran is unknown."
)


def settle_unstarted_execution(execution_id: str, job_id: str, error: str) -> None:
    """Close the receipt of a run that never started: a ``claimed`` row never resolves. Best-effort
    so a ledger write cannot mask the failure the caller logs or re-raises."""
    try:
        finish_execution(execution_id, success=False, error=error)
    except (sqlite3.Error, OSError) as record_err:
        logger.error("Job '%s': failed to close execution receipt %s (%s): %s",
                     job_id, execution_id, error, record_err)


def recover_interrupted_executions() -> int:
    """Mark abandoned attempts unknown without scheduling retries: rows whose owner is provably
    dead, plus rows whose live owner holds a claim older than the derived stale bound (the
    process is not killed)."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    # Derived on the first live-owned row only: the bound reads config, and the idle gateway
    # tick must stay config-free (tests/cron/test_idle_tick_config_skip.py).
    stale_after: Optional[float] = None
    stale_after_resolved = False
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, claimed_at, progress_at
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            reason = _OWNER_GONE_REASON
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                # A live owner is normally a legitimately running job. A worker permanently
                # deadlocked (e.g. futex_wait behind a route/proxy flip, #115692) also passes
                # this check, so a claim SILENT for longer than the derived bound is treated
                # as wedged and released — the external-worker wait loop polls this ledger
                # for a terminal status, so the job can fire again. Silence is measured from
                # the owner's last ``progress_at`` stamp (the run monitor refreshes it while
                # the agent is active), so a healthy multi-hour run is never reclaimed while
                # it is still working. The wedged worker PROCESS is NOT terminated here
                # (leaked until host restart); rows owned by this process (process_id ==
                # _PROCESS_ID, in-process runs) are skipped above and remain out of scope.
                if not stale_after_resolved:
                    stale_after = _live_owner_stale_after_seconds()
                    stale_after_resolved = True
                if (
                    stale_after is None
                    or _stale_age_seconds(row["claimed_at"], row["progress_at"]) <= stale_after
                ):
                    continue
                reason = _OWNER_WEDGED_REASON
            handoff_started_at = row["handoff_started_at"]
            if (
                row["handoff_pending"]
                and handoff_started_at is not None
                and time.time() - float(handoff_started_at)
                < HANDOFF_ADOPTION_GRACE_SECONDS
            ):
                continue
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?,
                       handoff_pending=0, handoff_started_at=NULL
                   WHERE id=? AND status=? AND process_id=? AND pid=?
                     AND handoff_pending=?
                     AND handoff_started_at IS ?""",
                (now, reason, row["id"], row["status"], row["process_id"], row["pid"],
                 row["handoff_pending"], row["handoff_started_at"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _fetch(conn, row["id"])
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def terminalize_dead_owner(execution_id: str, *, reason: str) -> bool:
    """Record one attempt as ``unknown`` with a cause this process actually observed.

    ``recover_interrupted_executions`` sweeps every attempt whose owner is provably
    dead, and knows nothing but that absence — so all it can write is
    ``_OWNER_GONE_REASON``, which asserts a scheduler restart. A waiter that held the
    worker's ``Popen`` knows more: the owner was that external worker, and it exited
    with a known status. Without this, a manual run whose worker dies is filed as
    "Scheduler restarted ..." (a restart that never happened) and, because the sweep
    leaves the row terminal, the waiter reports success and never records the run —
    the job's ``fire_claim`` then blocks the next manual fire for the whole lease
    (#128509).

    The attempt stays ``unknown``, not ``failed``: whether side effects ran is still
    unknown. Only the CAUSE becomes truthful. Returns False — leaving the caller to
    fall back to the generic sweep — when the row is absent, already terminal, owned by
    this process, inside the handoff adoption grace, or owned by a live process: a
    worker that is still running must never be terminalized out from under itself.
    """
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at
               FROM executions WHERE id=?""",
            (execution_id,),
        ).fetchone()
        if row is None or row["status"] not in ("claimed", "running"):
            return False
        if row["process_id"] == _PROCESS_ID:
            return False
        if _owner_is_live(int(row["pid"]), row["process_started_at"]):
            return False
        handoff_started_at = row["handoff_started_at"]
        if (
            row["handoff_pending"]
            and handoff_started_at is not None
            and time.time() - float(handoff_started_at) < HANDOFF_ADOPTION_GRACE_SECONDS
        ):
            return False
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?""",
            (now, reason, row["id"], row["status"], row["process_id"], row["pid"]),
        )
        if cur.rowcount != 1:
            return False
        record = _fetch(conn, execution_id)
        _prune_unlocked(conn)
    _emit_execution_state(record)
    return True


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        # Same (instant, text) key as the ORDER BY, so a page never skips or repeats a row.
        clauses.append("(julianday(claimed_at), claimed_at) < (julianday(?), ?)")
        params.extend([str(before_claimed_at)] * 2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    # Stamps carry the local offset, which changes at DST and on a timezone change, so text order
    # is not time order. julianday() compares instants (ms); the text breaks same-ms ties.
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def live_inflight_execution(job_id: str) -> Optional[Dict[str, Any]]:
    """The job's latest attempt while it is still claimed/running under a LIVE owner, else ``None``.

    This is scheduler OWNERSHIP, not recent activity: a run inside a long tool call writes no
    heartbeat yet stays owned, while a run whose process died (watchdog kill, crash) does not.
    Read-only — unlike ``recover_interrupted_executions`` it never rewrites a row.
    """
    record = latest_execution(job_id)
    if not record or record.get("status") not in ("claimed", "running"):
        return None
    if not _owner_is_live(int(record["pid"]), record.get("process_started_at")):
        return None
    return record


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    # One windowed sort: a per-row correlated ORDER BY julianday() cannot use the index and
    # grows quadratically with history (~90 ms at 1000 rows).
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e WHERE e.id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                             PARTITION BY job_id
                             ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC
                           ) AS rn
                    FROM executions WHERE job_id IN ({placeholders}))
                  WHERE rn=1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
