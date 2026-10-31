"""Explicit host-only owner action; never a tool, hook, CLI or automatic turn action.

Offline preparation. A release owner must separately admit the trusted invocation.
A durable turn lease proves exclusivity, NOT permission to reconcile a recovery.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import time

from hermes_constants import get_hermes_home
from tools.mcp_tool_pax_recovery import RecoveryReadError, require

_BINDING = Path(__file__).resolve().parents[1] / "integrations/pax-recovery-native/binding.mjs"
_ACTION_TIMEOUT = 45.0


class OwnerFence:
    """Stable native owner/DB/current-turn identity, distinct from historical evidence."""

    def __init__(self, lease):
        self.lease = lease
        self.agent = lease.agent
        self.db = lease.db
        self.home = get_hermes_home().resolve()
        self.session_id = lease.session_id
        self.holder = lease.holder
        self.deadline = time.monotonic() + _ACTION_TIMEOUT
        self.stopped = False
        self.check()

    def check(self):
        from tools import mcp_tool_pax_recovery as reads

        require(not self.stopped, "owner_stopped")
        require(get_hermes_home().resolve() == self.home == reads.HOME, "owner_profile_mismatch")
        require(Path(self.db.db_path).resolve() == self.home / "state.db", "owner_db_mismatch")
        require(self.agent._session_db is self.db and self.lease.agent is self.agent
                and self.lease.db is self.db, "owner_changed")
        require(self.session_id and self.agent.session_id == self.session_id
                and self.lease.session_id == self.session_id, "owner_session_changed")
        require(self.lease.holder == self.holder
                and self.agent._active_session_turn_lease_holder == self.holder, "owner_lease_changed")
        require(self.lease.is_turn_active() and not self.lease.stop.is_set()
                and not self.agent._interrupt_requested, "owner_inactive")
        require(time.time() < self.lease._authority_deadline
                and time.monotonic() < self.deadline, "owner_expired")
        expiry = self.db.session_turn_lease_expires_at(self.session_id, self.holder)
        require(isinstance(expiry, (int, float)) and expiry > time.time(), "owner_lease_lost")


def run_owner_action(lease):
    """New dedicated native entry. Call only from a separately admitted trusted owner action.

    No caller-supplied endpoint, tool, job, source, session, code, path or identity.
    The owning agent consumes admission under its existing process-stable lock,
    including on failure; replacing a reader or taking another turn cannot reset it.
    """
    from agent.turn_facade_lease import DurableTurnLease

    require(isinstance(lease, DurableTurnLease), "owner_lease_invalid")
    with lease.agent._pending_redirect_lock:
        require(not getattr(lease.agent, "_pax_recovery_consumed", False), "owner_already_consumed")
        lease.agent._pax_recovery_consumed = True
    fence = OwnerFence(lease)
    return _run(fence)


def _run(fence):
    from agent.memory_provider import spawn_context_thread
    from tools.environments.local import served_profile_child_env

    from tools.mcp_tool_pax_recovery import open_reads, serve_channel

    reader = open_reads(owner_check=fence.check)
    # Resolve and validate before spawning, never impersonate a home to pass checks.
    fence.check()
    env = served_profile_child_env(
        base={"PATH": "/usr/bin:/bin", "HOME": str(fence.home), "LANG": "C.UTF-8"},
        target_home=fence.home, inherit_credentials=False,
    )
    left, right = socket.socketpair()
    child = None
    thread = None
    errors = []

    def serve():
        try:
            serve_channel(left, reader)
        except BaseException as exc:
            errors.append(exc)

    try:
        fence.check()
        program = (
            "import net from 'node:net';"
            f"import {{reconcileFixedPaxRecovery}} from {json.dumps(_BINDING.as_uri())};"
            "const socket=new net.Socket({fd:Number(process.argv[1]),readable:true,writable:true});"
            "try { const result=await reconcileFixedPaxRecovery(socket);"
            "process.stdout.write(JSON.stringify(result)); } finally { socket.destroy(); }"
        )
        child = subprocess.Popen(
            ["/usr/bin/node", "--input-type=module", "-e", program, str(right.fileno())],
            pass_fds=(right.fileno(),), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        right.close()
        thread = spawn_context_thread(serve, name="pax-recovery-owned-read")
        thread.start()
        while child.poll() is None:
            fence.check()
            # Polling is also the stop fence when no socket request is in flight.
            try:
                output, error = child.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            output, error = child.communicate()
        fence.check()
        require(child.returncode == 0, "consumer_failed")
        require(not errors, "host_failed")
        return json.loads(output)
    finally:
        # A possible consumer write is never rolled back here. Preserve evidence;
        # stop is terminal and admission remains consumed even after partial effects.
        fence.stopped = True
        reader.stopped = True
        right.close()
        try:
            left.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # The serving thread can already have closed the same socket.
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=2)
        try:
            # Cleanup must not itself be cancelled by the turn's interrupt flag.
            # It uses only the existing loop, never discovery or a new process.
            from tools.mcp_tool_loop import _running_loop
            loop = _running_loop()
            require(loop is not None, "cleanup_loop_unavailable")
            asyncio.run_coroutine_threadsafe(reader.close(), loop).result(timeout=3)
        finally:
            try:
                if thread is not None:
                    thread.join(timeout=13)
                    if thread.is_alive():
                        raise RecoveryReadError("pax_native_read_host_did_not_stop")
            finally:
                left.close()
