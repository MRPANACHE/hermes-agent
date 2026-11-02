"""Independent source feedback lands on one idle card, idempotently."""
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_measured_outcome import apply_measured_outcome

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as db:
        yield db

def waiting(conn):
    tid = kb.create_task(conn, title="Repair source", assignee="owner")
    task = kb.claim_task(conn, tid)
    run = task.current_run_id
    assert kb.schedule_task(conn, tid, reason="OOS independently measures recovery", expected_run_id=run)
    return dict(task_id=tid, profile="owner", expected_run_id=run,
                evidence_ref="oos://cases/c/events/1", evidence_refs=["oos://measurements/1"],
                reason="Status: Onafhankelijke bronmeting.")

@pytest.mark.parametrize("action,status", [("close", "done"), ("reopen", "ready")])
def test_same_card_and_replay(conn, action, status):
    args = waiting(conn)
    count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    result = apply_measured_outcome(conn, **args, action=action)
    assert result["applied"] and result["status"] == status
    assert kb.get_task(conn, args["task_id"]).assignee == "owner"
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == count
    assert apply_measured_outcome(conn, **args, action=action)["replay"] is True
    events = [e for e in kb.list_events(conn, args["task_id"]) if e.kind == "measured_outcome"]
    assert len(events) == 1
    if action == "reopen":
        comments = kb.list_comments(conn, args["task_id"])
        assert len([c for c in comments if "ACE-leerroute" in c.body]) == 1

@pytest.mark.parametrize("mutation", ["active", "new_run", "human", "owner", "claim"])
def test_stale_feedback_never_overwrites_changed_work(conn, mutation):
    args = waiting(conn)
    tid = args["task_id"]
    with kb.write_txn(conn):
        if mutation == "active":
            conn.execute("UPDATE task_runs SET ended_at=NULL WHERE id=?", (args["expected_run_id"],))
        elif mutation == "new_run":
            conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at) VALUES (?,?, 'done',1,2)", (tid, "owner"))
        elif mutation == "human":
            conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (tid,))
        elif mutation == "owner":
            conn.execute("UPDATE tasks SET assignee='another' WHERE id=?", (tid,))
        else:
            conn.execute("UPDATE tasks SET claim_lock='claimed' WHERE id=?", (tid,))
    before = kb.get_task(conn, tid).status
    assert apply_measured_outcome(conn, **args, action="close")["applied"] is False
    assert kb.get_task(conn, tid).status == before


@pytest.mark.parametrize("action,status", [("close", "done"), ("reopen", "ready")])
@pytest.mark.parametrize("relative", [False, True])
def test_module_entrypoint_opens_argv_path(conn, tmp_path, monkeypatch, capsys, action, status, relative):
    import json
    from hermes_cli.kanban_db_measured_outcome import main

    args = waiting(conn)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.chdir(Path(db_path).parent)
    argv_path = Path(db_path).name if relative else db_path
    main([argv_path, json.dumps({**args, "action": action})])
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is True
    assert kb.get_task(conn, args["task_id"]).status == status
    main([argv_path, json.dumps({**args, "action": action})])
    assert json.loads(capsys.readouterr().out)["replay"] is True


def test_conflicting_replay_refused(conn):
    args = waiting(conn)
    apply_measured_outcome(conn, **args, action="close")
    with pytest.raises(ValueError, match="conflict"):
        apply_measured_outcome(conn, **args, action="reopen")
