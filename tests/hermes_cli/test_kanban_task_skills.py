"""Same-card skill repair preserves history and never mutates an active launch."""
import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban, kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    with kb.connect() as db:
        yield db


def repair(conn, tid, skills=(), reason="remove unavailable launcher binding"):
    from hermes_cli.kanban_task_skills import set_task_skills
    return set_task_skills(conn, tid, skills, actor="maintainer", reason=reason)


def snapshot(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def blocked_card(conn):
    tid = kb.create_task(conn, title="original", body="draft only; no merge/deploy", assignee="builder",
                         skills=["unavailable-skill"])
    claim = kb.claim_task(conn, tid)
    assert claim
    assert kb.block_task(conn, tid, kind="needs_input", reason="launcher failed",
                         expected_run_id=claim.current_run_id)
    parent = kb.create_task(conn, title="parent")
    kb.link_tasks(conn, parent, tid)
    return tid


def test_clear_preserves_card_graph_runs_history_and_is_idempotent(conn):
    tid = blocked_card(conn)
    before = snapshot(conn, tid)
    runs = [tuple(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))]
    links = [tuple(r) for r in conn.execute("SELECT * FROM task_links")]
    events = [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))]
    assert repair(conn, tid)
    after = snapshot(conn, tid)
    assert json.loads(after.pop("skills")) == []
    before.pop("skills")
    assert after == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))] == runs
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_links")] == links
    new_events = list(conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,)))
    assert [tuple(r) for r in new_events[:-1]] == events
    event = new_events[-1]
    assert event["kind"] == "skills_updated"
    assert event["run_id"] is None
    assert json.loads(event["payload"]) == {
        "before": ["unavailable-skill"], "after": [], "actor": "maintainer",
        "reason": "remove unavailable launcher binding",
    }
    assert repair(conn, tid)
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))] == [tuple(r) for r in new_events]


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("status", "review"), ("status", "done"), ("status", "archived"),
    ("claim_lock", "foreign-claim"), ("claim_expires", 1),
    ("current_run_id", 999), ("worker_pid", 123), ("worker_started_at", "fingerprint"),
])
def test_active_or_terminal_card_refused_without_writes(conn, field, value):
    tid = blocked_card(conn)
    conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, tid))
    conn.commit()
    before = snapshot(conn, tid)
    count = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    with pytest.raises(RuntimeError):
        repair(conn, tid)
    assert snapshot(conn, tid) == before
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == count


@pytest.mark.parametrize("worker", ["same", "foreign"])
def test_worker_cannot_change_any_launch_metadata(conn, monkeypatch, worker):
    tid = blocked_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid if worker == "same" else "t_foreign")
    before = snapshot(conn, tid)
    with pytest.raises(PermissionError, match="orchestrator-only"):
        repair(conn, tid)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize("skills", [["terminal"], ["one,two"], "not-a-list", [42]])
def test_invalid_skill_input_is_rejected(conn, skills):
    tid = blocked_card(conn)
    before = snapshot(conn, tid)
    with pytest.raises((TypeError, ValueError)):
        repair(conn, tid, skills)
    assert snapshot(conn, tid) == before


def test_reason_and_actor_required_and_normalization_matches_create(conn):
    tid = blocked_card(conn)
    with pytest.raises(ValueError):
        repair(conn, tid, reason=" ")
    assert repair(conn, tid, [" alpha ", "alpha", "beta"])
    assert kb.get_task(conn, tid).skills == ["alpha", "beta"]
    assert not repair(conn, "t_missing")


def test_cli_parser_handler_clear_and_set_readback(conn, capsys):
    tid = blocked_card(conn)
    parser = kanban.build_parser(argparse.ArgumentParser().add_subparsers(dest="command"))
    args = parser.parse_args(["set-skills", tid, "--clear", "--reason", "launcher correction"])
    assert kanban._HANDLERS["set-skills"](args) == 0
    assert kb.get_task(conn, tid).skills == []
    args = parser.parse_args(["set-skills", tid, "--skill", "alpha", "--skill", "beta",
                              "--reason", "explicit replacement"])
    assert kanban._HANDLERS["set-skills"](args) == 0
    assert kb.get_task(conn, tid).skills == ["alpha", "beta"]
    assert "skills" in capsys.readouterr().out.lower()


def test_cli_worker_refusal_returns_error(conn, monkeypatch, capsys):
    tid = blocked_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_foreign")
    parser = kanban.build_parser(argparse.ArgumentParser().add_subparsers(dest="command"))
    args = parser.parse_args(["set-skills", tid, "--clear", "--reason", "launcher correction"])
    assert kanban._HANDLERS["set-skills"](args) != 0
    assert kb.get_task(conn, tid).skills == ["unavailable-skill"]
    assert "orchestrator-only" in capsys.readouterr().err


def test_open_run_refused_even_without_card_claim(conn):
    tid = blocked_card(conn)
    conn.execute("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError):
        repair(conn, tid)


def test_audit_failure_rolls_back_metadata(conn, monkeypatch):
    tid = blocked_card(conn)
    before = snapshot(conn, tid)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(kb, "_append_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        repair(conn, tid)
    assert snapshot(conn, tid) == before


def test_descendant_fence_remains_effective(conn, monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    tid = blocked_card(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(Path(path).parent))
    before = snapshot(conn, tid)
    with pytest.raises(PermissionError, match="delegate_task"):
        repair(conn, tid)
    assert snapshot(conn, tid) == before


def test_correction_does_not_resume_and_next_normal_claim_uses_corrected_skills(conn):
    tid = kb.create_task(conn, title="same draft", body="no merge/deploy", assignee="builder",
                         skills=["unavailable-skill"], initial_status="blocked")
    assert repair(conn, tid)
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.claim_task(conn, tid) is None
    assert kb.unblock_task(conn, tid)
    claim = kb.claim_task(conn, tid)
    assert claim and claim.skills == [] and claim.assignee == "builder"
    assert claim.body == "no merge/deploy"
    assert claim.current_run_id is not None
    assert kb.claim_task(conn, tid) is None


@pytest.mark.parametrize("flags", [[], ["--clear", "--skill", "alpha"]])
def test_cli_requires_explicit_unambiguous_replacement(conn, flags):
    tid = blocked_card(conn)
    parser = kanban.build_parser(argparse.ArgumentParser().add_subparsers(dest="command"))
    args = parser.parse_args(["set-skills", tid, "--reason", "test", *flags])
    assert kanban._HANDLERS["set-skills"](args) == 2
    assert kb.get_task(conn, tid).skills == ["unavailable-skill"]
