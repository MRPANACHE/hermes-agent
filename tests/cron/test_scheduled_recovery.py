"""Bounded recovery preserves owner, exact failure lineage, idempotency and cadence."""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from cron import executions, jobs


def _blocked_board(path):
    metadata = {
        "schema": "mrpanache.agent-request.v1", "agent": "otto", "kind": "operation",
        "delivery_goal": "verified_live", "fingerprint": "a" * 64, "conversation_id": "conversation-1",
    }
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE tasks (id TEXT, body TEXT, assignee TEXT, status TEXT, current_run_id INTEGER);
            CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, run_id INTEGER, payload TEXT);
            CREATE TABLE task_runs (id INTEGER, task_id TEXT, outcome TEXT);
        """)
        conn.execute("INSERT INTO tasks VALUES (?,?,?,'blocked',NULL)",
                     ("task-1", json.dumps(metadata), "otto"))
        conn.execute("INSERT INTO task_runs VALUES (1,'task-1','blocked')")
        conn.execute("INSERT INTO task_events VALUES (1,'task-1','blocked',1,'{}')")
        return executions.recovery_lineage_snapshot(conn, "task-1")


@pytest.fixture
def recovery_case(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(owner))
    now = datetime(2026, 10, 11, 8, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    board = tmp_path / "board.db"
    lineage = _blocked_board(board)
    monkeypatch.setattr("hermes_cli.kanban_db.kanban_db_path", lambda **_kwargs: board)
    with jobs.use_cron_store(owner):
        job = jobs.create_job(prompt="Assess the current evidence", schedule="0 9 * * *")
        expiry = job["next_run_at"]
        failure = executions.create_execution(
            job["id"], source="builtin", recovery_expires_at=expiry,
            recovery_scope_hash=executions.recovery_job_scope(job),
        )
        executions.record_execution_evidence(failure["id"], session_id="failed-session")
        executions.finish_execution(failure["id"], success=False, error="explicit failure")
        jobs.mark_job_run(job["id"], False, "explicit failure")
        binding = {
            "failure_id": failure["id"], "job_id": job["id"], "owner_home": str(owner.resolve()),
            "job_scope_hash": executions.recovery_job_scope(job), "failed_session_id": "failed-session",
            "failure_expires_at": expiry, "next_run_at": expiry, "lineage": lineage,
        }
        yield owner, job, binding, board


def test_storage_only_reads_do_not_create_or_change_profile(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    caller = tmp_path / "caller"
    owner = tmp_path / "absent-owner"
    monkeypatch.setenv("HERMES_HOME", str(caller))
    with executions.use_execution_store(owner):
        assert executions.latest_scheduled_failure("job") is None
        assert executions.scheduled_recovery_status("failure") is None
        assert executions.scheduled_recovery_by_ref("a" * 64) is None
        assert executions.admitted_scheduled_recoveries() == []
        assert get_hermes_home() == caller
    assert not owner.exists()


def test_recovery_claim_and_terminal_write_preserve_cadence_and_evidence(recovery_case):
    owner, job, binding, _board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    claimed, prompt = executions.claim_scheduled_recovery(binding["failure_id"])
    assert prompt == "New verified evidence"
    assert claimed["_scheduled_instant"] is None
    assert jobs.get_job(job["id"])["next_run_at"] == binding["next_run_at"]
    assert executions.mark_execution_running(claimed["execution_id"]) is not None
    output = jobs.save_job_output(job["id"], "Recovery execution evidence")
    executions.record_execution_evidence(claimed["execution_id"], session_id="recovery-session", output_path=output)
    assert jobs.mark_job_run(
        job["id"], False, "Still unavailable", expected_fire_owner=claimed["fire_claim"]["by"],
        model_unreachable=True,
    )
    executions.finish_execution(claimed["execution_id"], success=False, error="Still unavailable")
    assert jobs.get_job(job["id"])["next_run_at"] == binding["next_run_at"]
    record = executions.scheduled_recovery_status(binding["failure_id"])
    assert record["execution"]["session_id"] == "recovery-session"
    assert record["execution"]["output_path"] == str(output)
    assert record["execution"]["source"] == "scheduled_recovery"
    assert executions.claim_scheduled_recovery(binding["failure_id"]) is None


def test_binding_tombstone_survives_attempt_pruning(recovery_case, monkeypatch):
    _owner, _job, binding, _board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    claimed, _prompt = executions.claim_scheduled_recovery(binding["failure_id"])
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 0)
    executions.finish_execution(claimed["execution_id"], success=True)
    assert executions.get_execution(binding["failure_id"]) is None
    repeated = executions.admit_scheduled_recovery(binding, "New verified evidence")
    assert repeated["phase"] == "possibly_fired"
    assert executions.claim_scheduled_recovery(binding["failure_id"]) is None
    with pytest.raises(ValueError, match="idempotency_conflict"):
        executions.admit_scheduled_recovery(binding, "Different mandate")
    ref = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert executions.scheduled_recovery_by_ref(ref)["execution"] is None


def test_recovery_revalidates_changed_job_and_blocked_lineage(recovery_case):
    _owner, job, binding, board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    jobs.update_job(job["id"], {"prompt": "Changed mandate"})
    with pytest.raises(ValueError, match="scope_changed"):
        executions.claim_scheduled_recovery(binding["failure_id"])
    jobs.update_job(job["id"], {"prompt": job["prompt"]})
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET status='ready'")
    with pytest.raises(ValueError, match="lineage_changed"):
        executions.claim_scheduled_recovery(binding["failure_id"])
    assert executions.scheduled_recovery_status(binding["failure_id"])["phase"] == "admitted"


def test_recovery_cannot_execute_from_callers_profile(recovery_case, monkeypatch, tmp_path):
    owner, _job, binding, _board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "caller"))
    with executions.use_execution_store(owner):
        with pytest.raises(ValueError, match="owner_mismatch"):
            executions.claim_scheduled_recovery(binding["failure_id"])


def test_recovery_requires_cross_process_lock_even_when_nested(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_acquire_flock", lambda *_args: None)
    with jobs.use_cron_store(tmp_path):
        with jobs._jobs_lock():
            with pytest.raises(RuntimeError, match="cross-process jobs lock"):
                with jobs._jobs_lock(strict=True):
                    pass


def test_recovery_expiry_is_inclusive(recovery_case, monkeypatch):
    _owner, _job, binding, _board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    boundary = datetime.fromisoformat(binding["failure_expires_at"])
    monkeypatch.setattr(executions, "_hermes_now", lambda: boundary)
    with pytest.raises(ValueError, match="stale_scheduled_failure"):
        executions.claim_scheduled_recovery(binding["failure_id"])
    assert executions.scheduled_recovery_status(binding["failure_id"])["phase"] == "admitted"


def test_recovery_context_travels_native_worker_handoff(recovery_case, monkeypatch):
    from cron import scheduler

    _owner, job, binding, _board = recovery_case
    executions.admit_scheduled_recovery(binding, "New verified evidence")
    claimed, prompt = executions.claim_scheduled_recovery(binding["failure_id"])
    captured = {}

    def handoff(snapshot):
        captured.update(snapshot)
        return True

    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", handoff)
    assert scheduler.run_one_job(
        claimed, extra_prompt=prompt, preserve_next_run_at=binding["next_run_at"]
    )
    assert captured["_scheduled_recovery_prompt"] == prompt
    assert captured["_preserve_next_run_at"] == binding["next_run_at"]
    assert captured["fire_claim"]["preserve_next_run_at"] == binding["next_run_at"]
    assert "_scheduled_recovery_prompt" not in jobs.get_job(job["id"])
