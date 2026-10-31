"""Offline owner admission uses real lease rows; no production profile or MCP.

Tests call the explicit trusted-host API, never model dispatch or startup hooks.
"""
import threading
import time
from types import SimpleNamespace as NS

import pytest

from agent import pax_recovery_owner as owner
from agent.turn_facade_lease import DurableTurnLease
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools import mcp_tool_pax_recovery as reads


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    home = tmp_path / "pax"
    home.mkdir()
    token = set_hermes_home_override(home)
    monkeypatch.setattr(reads, "HOME", home)
    db = SessionDB(home / "state.db")
    session = "synthetic-current-owner-session"
    holder = "synthetic-owner-holder"
    db.create_session(session, "cli")
    assert db.acquire_session_turn_lease(session, holder, wait_seconds=0)
    agent = NS(_session_db=db, session_id=session, _active_session_turn_lease_holder=holder,
               _interrupt_requested=False, _pending_redirect_lock=threading.Lock())
    lease = DurableTurnLease(agent, db, session, holder)
    lease.turn_active = True
    try:
        yield home, agent, lease
    finally:
        # Restore only the synthetic metadata fault before closing its real DB.
        db.db_path = home / "state.db"
        lease.release()
        db.close()
        reset_hermes_home_override(token)


def test_explicit_lease_entry_is_one_shot_across_turns(admitted, monkeypatch):
    home, agent, lease = admitted
    calls = []
    monkeypatch.setattr(owner, "_run", lambda fence: calls.append(fence) or "synthetic-result")
    assert lease.run_pax_recovery_owner_action() == "synthetic-result"
    assert calls[0].session_id != reads.NATIVE_SESSION
    replacement = DurableTurnLease(agent, lease.db, lease.session_id, lease.holder)
    replacement.turn_active = True
    with pytest.raises(reads.RecoveryReadError, match="already_consumed"):
        replacement.run_pax_recovery_owner_action()
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["profile", "db", "holder", "session", "inactive", "interrupt", "expiry", "lost-row"])
def test_owner_change_is_terminal_before_effect(admitted, monkeypatch, fault):
    home, agent, lease = admitted
    def forbidden(_):
        pytest.fail("No child/socket/read may start after an invalid owner fence")
    monkeypatch.setattr(owner, "_run", forbidden)
    if fault == "profile":
        monkeypatch.setattr(owner, "get_hermes_home", lambda: home.parent)
    elif fault == "db":
        monkeypatch.setattr(lease.db, "db_path", home.parent / "state.db")
    elif fault == "holder":
        agent._active_session_turn_lease_holder = "other"
    elif fault == "session":
        agent.session_id = reads.NATIVE_SESSION
    elif fault == "inactive":
        lease.turn_active = False
    elif fault == "interrupt":
        agent._interrupt_requested = True
    elif fault == "expiry":
        lease._authority_deadline = time.time() - 1
    else:
        lease.db.release_session_turn_lease(lease.session_id, lease.holder)
    with pytest.raises(reads.RecoveryReadError):
        lease.run_pax_recovery_owner_action()
    assert agent._pax_recovery_consumed is True
    with pytest.raises(reads.RecoveryReadError, match="already_consumed"):
        lease.run_pax_recovery_owner_action()


def test_failure_never_rearms_admission(admitted, monkeypatch):
    _, agent, lease = admitted
    def fail(_):
        raise TimeoutError("synthetic timeout; effect status unknown")
    monkeypatch.setattr(owner, "_run", fail)
    with pytest.raises(TimeoutError):
        lease.run_pax_recovery_owner_action()
    assert agent._pax_recovery_consumed
    with pytest.raises(reads.RecoveryReadError, match="already_consumed"):
        lease.run_pax_recovery_owner_action()


def test_concurrent_owner_admission_is_atomic(admitted, monkeypatch):
    _, agent, lease = admitted
    entered = threading.Event()
    release = threading.Event()
    errors = []
    def running(_):
        entered.set()
        assert release.wait(timeout=3)
        return "synthetic"
    monkeypatch.setattr(owner, "_run", running)
    from agent.memory_provider import spawn_context_thread
    def first():
        try:
            lease.run_pax_recovery_owner_action()
        except BaseException as exc:
            errors.append(exc)
    thread = spawn_context_thread(first, name="offline-owner-test")
    thread.start()
    try:
        assert entered.wait(timeout=3)
        with pytest.raises(reads.RecoveryReadError, match="already_consumed"):
            lease.run_pax_recovery_owner_action()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not errors
