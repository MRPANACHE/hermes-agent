Pax dedicated native recovery reads — PREPARATION ONLY

Authority
  Platform support t_d11e1a13 / native run1346 for existing owner t_0f81eef8.
  Mara's 30 October 2026 preparation disposition only. No live retry authority.
  No task/controller, per-server grant, config change, tool/command registration,
  plugin install, discovery, credential read/export, auth refresh, activation,
  bootstrap, restart, repin, push/PR, merge or deploy is included.

Source binding
  Hermes base 3a3e7269c028d3917d413395d96844309f046510, independently confirmed
  from this worker's process ancestry. Isolated clone, not the loaded release.
  Consumer clone 9ede9adbefa9c55b74c8e2311423f46ba0d91fa0, unchanged.
  Fixed installed consumer release 3d2ce28882926b7cef2066bc0d04ce7768605496.
  Its queue and receipt module bytes match the tested local consumer bytes.
  Fixed source business-pb; recovery5e5c7051b1058e0f9b03d07afac9ec0da593adb8aee4db0135432c4dddcaa192;
  historical native session20261027_214817_10339b.
  Fixed original raw and canonical hashes remain in binding.mjs; never test
  fixture hashes or returned history hashes in place of deployment binding.

Native composition
  Public runtime facade tools.mcp_tool.serve_pax_recovery_reads(fd), not a model
  tool, plugin entry, dispatcher, CLI, server, shellbridge or external import
  of a private handler. Runtime-owned mcp_tool_pax_recovery is a native client
  sibling following existing core ownership. PluginContext.call_mcp default
  deny and plugin grants are NOT changed or bypassed by a registered plugin.

  The host must already be the authenticated Pax process and own the inherited
  socket. open_reads accepts no parameters. It demands exact profile home,
  its OWN already-connected brain entry (not adopted), exact existing OAuth
  provider/endpoint, current in-memory valid read scope and fixed client id.
  Neither config nor token storage is loaded. Missing/stale binding stops.

  The factory does not start or resume a Pax process. A Node/shell child cannot
  import this method to obtain another process's MCP session. A subsequently
  authorized native host call site must make and inherit the private socket;
  no call site, listener, process launcher or activation is installed here.

  The typed channel carries only history or fact-by-index. No caller-supplied
  endpoint/tool/path/request/source/identity/history/facts. The host computes
  MCP verbs and params itself. Facts derive only from the directly returned,
  original persistent history. Native session metadata is fixed by the host.
  Curator is taken ONLY from that row and compared to the existing native
  provider's authenticated client identity. Policy/handoff/receipt never fills
  a missing field. Native CallToolResult is decoded before model/render/spill.

  Complete one-row/no-cursor history required, 1–4 add decisions, distinct ids,
  exact request/run/source/curator. Two fresh rounds maximum for the existing
  consumer's pre/post-audit validation, with unchanged history. No replay after
  any error. Real native RPC lock/inflight tracking and SDK session.call_tool
  are reused. No acquire/lazy-connect/recoverer/refresh or auth retry ladder.

  No-refresh is enforced BELOW the RPC wrapper too: existing native provider
  uses only its in-memory header method for a frozen read. Native HTTP request
  and response hooks fence exact request, endpoint, metadata, once-only send,
  redirects and non-2xx. A delayed marked request after timeout is rejected
  before normal OAuth initialization/disk reload. Unsupported/SSE/old loaded
  transport without installed wire fence fails closed. No credential copies.

  binding.mjs is the fixed FUTURE owner-only consumer binding, NOT a runnable
  entry point. It checks exact node, pinned seal and sealed JS source, original
  raw/canonical job, complete audit/no prior completion, and uses the existing
  queue reconcileHistoricalReceipt without consumer changes. No live paths
  from that wrapper were invoked in this preparation. An approved host caller,
  release qualification and fresh live preflight remain mandatory.

Offline evidence (not live proof)
  test_composition.py drives the actual public native-host facade over a real
  inherited socketpair into channel.mjs and unchanged consumer queue/SQLite.
  Only the remote native-session backend/identity is synthetic. Test state is
  private scratch state. CallToolResult/TextContent are the installed real SDK
  types, not response captures or production evidence. Fixed production ids
  are labels in synthetic fixtures, not assertions about real history.
  offline-consumer.mjs adapts the source-owned existing test fixture, replacing
  only the remote callVerb seam with the actual typed channel. It is TEST ONLY.

  22 integration scenarios: success; wrong/missing/empty curator; unauthorized
  reader; expired auth; request/source mismatch; zero/multiple/partial/truncated
  history; truncated JSON; decision mismatch; changed canonical job; immutable
  proof conflict; timeout; connection change; fact mismatch; linked recovery
  conflict; changed raw bytes; post-audit history change. All fail-closed cases
  retain failed synthetic jobs; positive case has unknown runtime and 1 audit.
  There are also unit probes for model-input rejection, exact profile, actual
  SDK aliases/is_error/structured content/completeness/overflow, frozen auth
  and wire selectors. No test asserts a historical live-runtime reconstruction.

  test_wire.py exercises the ACTUAL production owned-client hooks and ACTUAL
  SDK HTTPX auth generator against MockTransport HTTP200/401/403/302/500.
  Exactly one synthetic resource HTTP send each, no redirect/refresh/retry.
  Repeated/delayed marked requests fail. No network connection is made.

  Existing consumer suites separately: 81/81 pass; these are supplementary,
  NOT substituted for the new composition integration.

  Runtime full suite/CI and real native Pax preflight are not claimed. Tests
  use unittest on the existing pinned native Python/dependency environment;
  no PM/config/plugin install. Host Python3.13 lacks ruamel; pinned runtime
  Python3.14 plus its existing dependency path imports and runs the real code.

Review/release handoff
  Review exact candidate HEAD and diff, including provider and HTTP-hook changes
  and the fixed binding's unexecuted live paths. This preparation does not waive
  independent exact-head review. Mara must separately dispose release/live work.
  Fresh true preflight must prove actual loaded Hermes candidate + guarded native
  transport, existing owner identity/source/current token/session, installed sealed
  consumer, original native SQLite/raw/canonical/lock/audit state and normal KPI.
  If auth is stale, stop: this artifact never refreshes or requests new secrets.
  A newly required activation/reconnect/grant is not authorized by preparation.
  Rollback/stop belongs in that later release disposition; do not install this
  patch or invoke binding.mjs based on dependency completion alone.

  All existing consumer native/raw/canonical/complete-history/lock/audit/unknown
  predicates remain unchanged. Historical runtime unknown. No live recovery,
  no KPI improvement/case closure claim. Latest supplied canonical KPI remains
  0/open until ordinary target measurement; downstream t_41591f76 scope and graph
  are untouched. Pax still owns the substantive recovery outcome.
