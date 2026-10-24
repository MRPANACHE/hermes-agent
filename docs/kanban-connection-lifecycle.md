# Native connection lifecycle

`hermes_cli.kanban_db.CONNECTION_LIFECYCLE_SCHEMA` is
`hermes.connection_lifecycle.v1`. The installed owner module must provide both
functions below before an adapter can advertise this capability. A source PR
does not update an installed runtime or prove a live operation.

```python
set_connection_enabled(conn, *, binding, enabled, actor, request_id)
read_connection_lifecycle(conn, *, binding)
```

`binding` has exactly `agent_id`, `profile`, `repo`, and absolute normalized
`workspace`. These are matched against the original `mrpanache.agent-request.v1`
task body `agent` and `native_binding`, and the actual assignee and tenant.
The adapter must authenticate its existing operator bearer and verify the
current approved registry binding before invoking the owner. A saved address
does not grant execution, credentials, profile provisioning, or stop authority.

Apply accepts the existing operator `actor="human:david"`. Every active task
in the exact binding must have original owner David/Dolly; otherwise the whole
operation fails with `connection_owner_mismatch` before writes. The existing
David-only ingress without owner fields is recognized only when origin owner
fields are also absent. No Bram/Dobby lifecycle grant is introduced.

Apply runs in its own existing SQLite `write_txn`. It records admission in
`task_events`, cancels queued tasks and newly marked, provably unstarted
dispatcher claims, and appends an idempotent receipt. Reusing a request ID for
the same binding and payload returns the original receipt; a different payload
raises `connection_request_conflict`. No new workflow database is introduced.

Cancelled tasks and runs remain in history with terminal `cancelled` state.
Cancellation does not archive, signal a PID, clean a workspace, satisfy a parent
dependency, or promote children. Started work continues normally and may finish
after disabling. Re-enabling permits new claims and never revives cancelled work.

The dispatcher records `spawn_pending` at claim. Before any OS spawn it commits
`spawn_started` independently, then enters a second write transaction to check
current admission and serialize spawn plus PID persistence against cancellation.
A cancellation between those transactions prevents spawn, while retaining the
conservative crossed-boundary state. Crash, failed PID persistence, or failed
commit after fork cannot restore proof of an unstarted claim. Such work remains
running, consumes a dispatch slot, and requires explicit owner investigation;
the dispatcher does not retry the uncertain spawn. Legacy PID-less runs are
also treated as running. NULL PID alone is never proof of no execution.
Both transaction boundaries also compare the claimed owner, saved binding,
tenant and execution inputs against the current task. A concurrent task edit
cannot authorize a stale task snapshot under a newly changed address.
Existing TTL recovery may restore a ready status after losing a PID receipt.
The durable unresolved boundary still counts as running and blocks a new claim
or cancellation; a requeue alone does not prove the earlier child exited.
Existing recovery policy is otherwise unchanged.

Apply receipts contain `schema_version`, `binding`, `enabled`, `actor`,
`request_id`, `fingerprint`, `before` and `after` counts (`queued`, `claimed`,
`running`), `cancelled_task_ids`, `running_task_ids`, `stop_required`,
`unbound_task_ids`, `scope_complete`, `observed_at`, and `event_id`.
Read returns current `enabled`, `counts`, running IDs, scope fields,
`stop_required`, and `latest_receipt` under one read transaction. Both functions
require a connection without an existing transaction. An idempotent receipt is
historical evidence; use read for the current state.

Active same-profile legacy or malformed tasks without an attributable binding
are left untouched and reported through `unbound_task_ids` with
`scope_complete=false`. Zero exact-bound tasks does not prove zero legacy work.

Tests use real temporary SQLite boards and fake spawn functions; no profiles,
credentials, workers, releases, or live services are modified.
