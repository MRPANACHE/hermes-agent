import { readFileSync, mkdirSync, openSync, closeSync, writeFileSync, renameSync, lstatSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import Database from 'better-sqlite3'
import { DEFAULT_OWNER_PROFILES } from '../pb/agent-registration.js'

const TECHNICAL_OWNERS = new Set(['otto', 'pax'])

export function validateTechnicalOwner (target = 'otto') {
  if (!TECHNICAL_OWNERS.has(target)) throw new Error('ace_technical_owner_invalid')
  return target
}

// Owner sessions get the same process basics as a Hermes gateway. They never get
// this transport's provider configuration or its loaded GBrain credentials.
const OWNER_ENV_KEYS = ['PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM', 'TMPDIR', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK', 'GPG_AGENT_INFO']

export function ownerProcessEnv (profileRoot, target, env = process.env) {
  const home = join(profileRoot, target)
  const selected = Object.fromEntries(OWNER_ENV_KEYS.filter(key => typeof env[key] === 'string').map(key => [key, env[key]]))
  // A Codex home is kept only when it already belongs to the target profile.
  if (typeof env.CODEX_HOME === 'string' && resolve(env.CODEX_HOME).startsWith(`${resolve(home)}/`)) selected.CODEX_HOME = env.CODEX_HOME
  return { ...selected, HERMES_HOME: home }
}

function validateWorkspace (cwd) {
  if (typeof cwd !== 'string' || !isAbsolute(cwd) || cwd.includes('\0')) throw new Error('ace_owner_workspace_invalid')
  return cwd
}

function configuredOwnerWorkspace (target, { profileRoot = '/home/david/.hermes/profiles', python = '/home/david/.local/share/hermes-otto-runtime/venv/bin/python', commandPrefix = ['-m', 'hermes_cli.main'], execute = execFileSync } = {}) {
  validateTechnicalOwner(target)
  let cwd
  try {
    cwd = JSON.parse(execute(python, [...commandPrefix, 'config', 'get', 'terminal.cwd', '--json'], {
      env: ownerProcessEnv(profileRoot, target),
      encoding: 'utf8', timeout: 15_000, maxBuffer: 16_384, stdio: ['ignore', 'pipe', 'pipe']
    }))
  } catch { throw new Error('ace_owner_workspace_unavailable') }
  return validateWorkspace(cwd)
}

export function technicalOwnerIdentity (target = 'otto', { resolveWorkspace = configuredOwnerWorkspace } = {}) {
  validateTechnicalOwner(target)
  return { target, session_id: null, cwd: validateWorkspace(resolveWorkspace(target)) }
}

function processInfo (pid, read = readFileSync) {
  const fields = read(`/proc/${pid}/stat`, 'utf8').split(') ').slice(1).join(') ').split(' ')
  return { pid, parent: Number(fields[1]), start_ticks: fields[19] }
}

export function captureDispatcherProcess () {
  const { pid, start_ticks: startTicks } = processInfo(process.pid)
  return { pid, start_ticks: startTicks }
}

export function dispatcherProcessAlive (stamp) {
  if (!Number.isSafeInteger(stamp?.pid) || stamp.pid < 1 || !/^[0-9]+$/u.test(stamp.start_ticks || '')) throw new Error('ace_dispatcher_process_invalid')
  try { return processInfo(stamp.pid).start_ticks === stamp.start_ticks } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ESRCH') return false
    throw error
  }
}

function handoffMarker (task, id) { return `${task.recovery_id ? 'PB proposal technical' : 'ACE operational'} handoff ${id}` }

// Hermes inserts a watchdog between the native agent and its stdio server.
// A watchdog restart is not an owner failure: retain the native ancestor.
export function captureNativeOwner ({ profile, ownerProfiles = DEFAULT_OWNER_PROFILES, parentPid = process.ppid, read = readFileSync } = {}) {
  if (!Object.values(ownerProfiles).includes(profile)) return null
  try {
    const parent = processInfo(parentPid, read)
    const args = read(`/proc/${parentPid}/cmdline`).toString().split('\0')
    if (args.some(arg => arg.endsWith('/tools/mcp_stdio_watchdog.py') || arg === 'tools.mcp_stdio_watchdog')) {
      const declared = Number(args[args.indexOf('--ppid') + 1])
      if (!declared || parent.parent !== declared) return null
      const native = processInfo(declared, read)
      return { pid: native.pid, start_ticks: native.start_ticks, profile }
    }
    if (profile !== 'noor') return null
    const argument = flag => args.includes(flag) ? args[args.indexOf(flag) + 1] : null
    if (!args.includes('/home/david/.local/share/hermes-otto-runtime/src/hermes_cli/main.py')
      || argument('--toolsets') !== 'noor_incident' || argument('--source') !== 'tool'
      || !/^noor-incident-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(argument('--continue') || '')) return null
    return { pid: parent.pid, start_ticks: parent.start_ticks, profile }
  } catch { return null }
}

function resultContext (content, incident = false) {
  if (typeof content !== 'string') return null
  try {
    const value = JSON.parse(content.slice(content.indexOf('{'), content.lastIndexOf('}') + 1))
    if (incident) {
      for (const block of value.content || []) {
        if (block.type !== 'text' || typeof block.text !== 'string') continue
        let context
        try { context = JSON.parse(block.text).ace_context } catch { continue }
        if (context?.schema_version === 'pb.ace_agent_context.v1' && context.agent_id === 'noor' && context.project_id === 'bol-bidbot') return context.context_id
      }
      return null
    }
    if (value.structuredContent?.context_id) return value.structuredContent.context_id
    if (value.context_id) return value.context_id
    if (typeof value.result === 'string') return JSON.parse(value.result).context_id
  } catch {}
  return null
}

function validateOwnerProfile (profile, ownerProfiles) {
  if (!Object.values(ownerProfiles).includes(profile)) throw new Error('ace_owner_identity_invalid')
}

function validateOwnerContext (profile, contextId, ownerProfiles) {
  validateOwnerProfile(profile, ownerProfiles)
  if (!/^[a-f0-9]{64}$/.test(contextId)) throw new Error('ace_owner_identity_invalid')
}

// A failed context reply never put its context_id in native tool history. The
// scheduler's existing receipt/query and the native terminal turn still prove
// its owner. This proves ended-without-result only; it cannot recover learning.
export function readTerminalIncident (task, { profileRoot = '/home/david/.hermes/profiles', incidentRoot = '/home/david/tmp/noor-incident-runtime', openDatabase = path => new Database(path, { readonly: true, fileMustExist: true }) } = {}) {
  const fail = () => { throw new Error('ace_close_terminal_incident_unproven') }
  const binding = task.binding
  if (task.owner_process || task.completion || task.checkpoint || task.receipt || binding?.agent_id !== 'noor' || binding.project_id !== 'bol-bidbot') fail()
  const { agent_id, project_id, session_id: runId, task_id: taskId } = binding
  const hash = value => createHash('sha256').update(value).digest('hex')
  const match = /^incident:([1-9]\d*):([1-9]\d*):turn:(noor-incident-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/u.exec(taskId || '')
  if (!match || match[3] !== runId || hash(JSON.stringify({ agent_id, project_id, session_id: runId, task_id: taskId })) !== task.context_id
    || task.task !== `Operational incident recovery for case ${match[1]} episode ${match[2]}; use bounded prior lessons as advisory context.`) fail()
  const directory = join(incidentRoot, runId)
  const bounded = (name, limit) => {
    const file = join(directory, name)
    const stat = lstatSync(file)
    if (!stat.isFile() || stat.size > limit) fail()
    return readFileSync(file, 'utf8')
  }
  const receiptBytes = bounded('receipt.json', 8192)
  const query = bounded('query.md', 65536)
  const receipt = JSON.parse(receiptBytes)
  if (receipt.exit_code !== 0 || typeof receipt.session_id !== 'string'
    || !query.endsWith(`Assigned scheduler run: ${runId}. Call incident_read to retrieve your case.\n`)) fail()
  const db = openDatabase(join(profileRoot, 'noor', 'state.db'))
  try {
    const session = db.prepare('SELECT id,source,end_reason,ended_at FROM sessions WHERE id=?').get(receipt.session_id)
    if (!session || !['cli', 'tool'].includes(session.source) || session.end_reason !== 'cli_close' || !Number.isFinite(session.ended_at)) fail()
    const first = db.prepare("SELECT id,content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1").get(session.id)
    const owners = db.prepare(`SELECT m.session_id FROM messages_fts f JOIN messages m ON m.id=f.rowid
      WHERE messages_fts MATCH ? AND m.role='user' AND m.content=?`).all(`content:"${runId}"`, query)
    if (first?.content !== query || owners.length !== 1 || owners[0].session_id !== session.id) fail()
    const tail = db.prepare('SELECT id,role,content,tool_calls FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1').get(session.id)
    if (tail?.role !== 'assistant' || !tail.content?.trim() || (tail.tool_calls && tail.tool_calls !== '[]')) fail()
    const reads = db.prepare("SELECT id,content FROM messages WHERE session_id=? AND role='tool' AND tool_name='incident_read' ORDER BY id LIMIT 101").all(session.id)
    if (reads.length > 100) fail()
    const source = reads.find(row => {
      try {
        const blocks = JSON.parse(row.content).content.filter(block => block.type === 'text').map(block => JSON.parse(block.text))
        return blocks.some(block => String(block.case?.id) === match[1] && String(block.case?.episode_id) === match[2])
          && blocks.some(block => block.ace_context_status === 'context_reply_invalid')
      } catch { return false }
    })
    if (!source) fail()
    return { run_id: runId, case_id: match[1], episode_id: match[2], session_id: session.id,
      source_sha: null, source_sha_verified: false,
      query_sha256: hash(query), receipt_sha256: hash(receiptBytes),
      first_message_id: first.id, incident_read_message_id: source.id, incident_read_sha256: hash(source.content),
      terminal_message_id: tail.id, terminal_message_sha256: hash(tail.content), ended_at: session.ended_at,
      evidence_refs: [`${join(directory, 'receipt.json')}#sha256=${hash(receiptBytes)}`, `${join(directory, 'query.md')}#sha256=${hash(query)}`,
        `hermes:noor:${session.id}:${source.id}`, `hermes:noor:${session.id}:${tail.id}`] }
  } finally { db.close() }
}

// Resolve only the requested context through the stored tool-output index.
// Never inspect reasoning or model-supplied session IDs.
export function createOwnerSessionResolver ({ profile, ownerProfiles = DEFAULT_OWNER_PROFILES, profileRoot = '/home/david/.hermes/profiles', kanbanDatabasePath = process.env.HERMES_KANBAN_DB || '/home/david/.hermes/kanban.db', openDatabase = path => new Database(path, { readonly: true, fileMustExist: true }) }) {
  validateOwnerProfile(profile, ownerProfiles)
  const db = openDatabase(join(profileRoot, profile, 'state.db'))
  let closed = false
  try {
    const toolPredicate = profile === 'noor' ? "(m.tool_name LIKE '%__ace_context' OR m.tool_name = 'incident_read')" : "m.tool_name LIKE '%__ace_context'"
    const selectContextMatches = db.prepare(`SELECT m.session_id, m.tool_name, m.content
      FROM messages_fts f JOIN messages m ON m.id = f.rowid
      WHERE messages_fts MATCH ? AND m.role='tool' AND ${toolPredicate}`)
    const selectSession = db.prepare('SELECT id, source, end_reason, cwd FROM sessions WHERE id = ?')
    const selectTail = db.prepare("SELECT role, content, tool_calls FROM messages WHERE session_id=? AND role IN ('assistant','tool','user') ORDER BY id DESC LIMIT 1")
    const selectReview = db.prepare("SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name='kanban_request_review' AND instr(content,?)>0 ORDER BY id DESC LIMIT 1")
    function nativeReviewContinuation (task, session, contextId) {
      if (!task || task.context_id !== contextId || task.completion || task.receipt || ownerProfiles[task.binding?.agent_id] !== profile || task.binding?.session_id !== session.id || !/^t_[a-f0-9]{8}$/u.test(task.binding?.task_id || '') || !isAbsolute(kanbanDatabasePath)) return false
      let board
      try {
        const content = selectReview.get(session.id, task.binding.task_id)?.content
        if (!content) return false
        const handoff = JSON.parse(content.slice(content.indexOf('{'), content.lastIndexOf('}') + 1))
        if (handoff.ok !== true || handoff.status !== 'review' || handoff.task_id !== task.binding.task_id || !Number.isSafeInteger(handoff.run_id)) return false
        board = openDatabase(kanbanDatabasePath)
        const review = board.prepare(`SELECT t.status,t.workspace_path,t.worker_pid,t.claim_lock,r.metadata,r.outcome
          FROM tasks t JOIN task_runs r ON r.task_id=t.id WHERE t.id=? AND r.id=?`).get(handoff.task_id, handoff.run_id)
        if (!review || review.outcome !== 'review_requested' || review.workspace_path !== session.cwd || review.worker_pid || review.claim_lock || JSON.parse(review.metadata || '{}').context_id !== task.context_id) return false
        const latestRequest = board.prepare("SELECT MAX(id) AS id FROM task_runs WHERE task_id=? AND outcome='review_requested'").get(handoff.task_id)
        if (latestRequest.id !== handoff.run_id) return false
        if (review.status === 'review') return true
        // A completed independent review ends the dependency, not the saved
        // original PB task. Only its exact original owner may finish learning.
        if (review.status !== 'done') return false
        return Boolean(board.prepare(`SELECT 1 FROM task_runs r JOIN task_events e ON e.task_id=r.task_id AND e.run_id=r.id
          WHERE r.task_id=? AND r.id>? AND r.outcome='completed' AND e.kind='claimed'
          AND json_extract(e.payload,'$.source_status')='review' LIMIT 1`).get(handoff.task_id, handoff.run_id))
      } catch { return false } finally { board?.close() }
    }
    return Object.freeze({
      resolve (contextId, task) {
        validateOwnerContext(profile, contextId, ownerProfiles)
        if (closed) throw new Error('ace_owner_resolver_closed')
        const matches = selectContextMatches.all(`content:"${contextId}"`)
          .filter(row => resultContext(row.content, row.tool_name === 'incident_read') === contextId)
        const ids = [...new Set(matches.map(row => row.session_id))]
        if (ids.length !== 1) throw new Error('ace_owner_session_unresolved')
        const session = selectSession.get(ids[0])
        const tail = selectTail.get(ids[0])
        const noorIncidentToolSession = profile === 'noor' && matches.every(row => row.tool_name === 'incident_read')
        if (!session || (session.source !== 'cli' && !(session.source === 'tool' && noorIncidentToolSession)) || ['compression', 'new_session', 'cron_complete'].includes(session.end_reason)) throw new Error('ace_owner_session_not_resumable')
        if (!tail || tail.role === 'user') throw new Error('ace_owner_session_finished_or_waiting')
        if (tail.role === 'assistant' && (!tail.tool_calls || tail.tool_calls === '[]') && tail.content?.trim() && !nativeReviewContinuation(task, session, contextId)) throw new Error('ace_owner_session_finished_or_waiting')
        return { session_id: session.id, cwd: session.cwd }
      },
      close () {
        if (closed) return
        closed = true
        db.close()
      }
    })
  } catch (error) {
    db.close()
    throw error
  }
}

// Search only stored tool output, never reasoning or model-supplied session IDs.
export function findOwnerSession ({ profile, contextId, task, ownerProfiles = DEFAULT_OWNER_PROFILES, profileRoot = '/home/david/.hermes/profiles', kanbanDatabasePath, openDatabase = path => new Database(path, { readonly: true, fileMustExist: true }) }) {
  validateOwnerContext(profile, contextId, ownerProfiles)
  const resolver = createOwnerSessionResolver({ profile, ownerProfiles, profileRoot, kanbanDatabasePath, openDatabase })
  try { return resolver.resolve(contextId, task) } finally { resolver.close() }
}

function completedHandoffSession (profileRoot, target, marker) {
  const db = new Database(join(profileRoot, target, 'state.db'), { readonly: true, fileMustExist: true })
  try {
    const rows = db.prepare("SELECT DISTINCT session_id FROM messages WHERE role='user' AND instr(content,?) > 0").all(marker)
    return rows.length === 1 ? rows[0].session_id : null
  } finally { db.close() }
}

// A technical handoff has its own session, rather than the original ACE context.
// Bind its workspace to the saved handoff marker, never today's profile default.
function technicalHandoffIdentity (profileRoot, target, sessionId, marker) {
  validateTechnicalOwner(target)
  const db = new Database(join(profileRoot, target, 'state.db'), { readonly: true, fileMustExist: true })
  try {
    const matches = db.prepare("SELECT DISTINCT session_id FROM messages WHERE role='user' AND instr(content,?)>0").all(marker)
    if (matches.length !== 1) throw new Error('ace_owner_session_unresolved')
    if (sessionId && matches[0].session_id !== sessionId) throw new Error('ace_owner_identity_invalid')
    const sessions = db.prepare('SELECT id, cwd FROM sessions WHERE id=?').all(matches[0].session_id)
    if (sessions.length !== 1) throw new Error('ace_owner_session_unresolved')
    return { target, session_id: sessions[0].id, cwd: validateWorkspace(sessions[0].cwd) }
  } finally { db.close() }
}

// Read the public assistant answer for this exact native handoff, never CLI
// reasoning or arbitrary sessions supplied by a tool caller.
export function readOwnerPublicReport ({ profileRoot = '/home/david/.hermes/profiles', ownerProfiles = DEFAULT_OWNER_PROFILES, target, sessionId, marker, recordedAt, openDatabase = path => new Database(path, { readonly: true, fileMustExist: true }) }) {
  if (!TECHNICAL_OWNERS.has(target)) validateOwnerProfile(target, ownerProfiles)
  if (!sessionId) return { status: 'unavailable', reason: 'native_session_missing' }
  const endedAt = typeof recordedAt === 'string' ? Date.parse(recordedAt) / 1000 : NaN
  if (!Number.isFinite(endedAt)) return { status: 'unavailable', reason: 'native_execution_time_missing' }
  const db = openDatabase(join(profileRoot, target, 'state.db'))
  try {
    const bound = db.prepare("SELECT id FROM messages WHERE session_id=? AND role='user' AND instr(content,?)>0 AND timestamp<=? ORDER BY id DESC LIMIT 1").get(sessionId, marker, endedAt)
    if (!bound) return { status: 'unavailable', reason: 'native_handoff_marker_missing' }
    // Hermes inserts internal user reminders during the same run. Its saved
    // execution timestamp, rather than the next user row, ends this window.
    const answer = db.prepare("SELECT id, content FROM messages WHERE session_id=? AND id>? AND timestamp<=? AND role='assistant' AND (tool_calls IS NULL OR tool_calls='[]') AND length(trim(content))>0 ORDER BY id DESC LIMIT 1").get(sessionId, bound.id, endedAt)
    return answer ? { status: 'available', message_ref: `hermes:${target}:${sessionId}:${answer.id}`, text: answer.content } : { status: 'unavailable', reason: 'native_public_answer_missing' }
  } finally { db.close() }
}

export function resolveMagnusThroughBroker (contextId, execute = execFileSync) {
  let result
  try {
    result = JSON.parse(execute('/usr/bin/sudo', ['-n', '/usr/local/bin/buzz-agent-broker', 'ace-owner-resolve'], { input: JSON.stringify({ context_id: contextId }), encoding: 'utf8', timeout: 15_000, maxBuffer: 16_384, env: { PATH: '/usr/bin:/bin' } }))
  } catch { throw new Error('ace_owner_broker_unavailable') }
  if (result.error && ['ace_owner_session_unresolved', 'ace_owner_session_not_resumable', 'ace_owner_session_finished_or_waiting'].includes(result.error)) throw new Error(result.error)
  if (typeof result.session_id !== 'string' || !result.session_id || typeof result.cwd !== 'string' || !result.cwd.startsWith('/')) throw new Error('ace_owner_broker_result_invalid')
  return { session_id: result.session_id, cwd: result.cwd }
}

export function createOwnerRecovery ({ profileRoot = '/home/david/.hermes/profiles', incidentRoot = '/home/david/tmp/noor-incident-runtime', ownerProfiles = DEFAULT_OWNER_PROFILES, python = '/home/david/.local/share/hermes-otto-runtime/venv/bin/python', commandPrefix = ['-m', 'hermes_cli.main'], spawnProcess = spawn, resolveSession = findOwnerSession, createSessionResolver = createOwnerSessionResolver, timeoutMs = 600_000, magnusBroker = profileRoot === '/home/david/.hermes/profiles', brokerResolve = resolveMagnusThroughBroker, execute = execFileSync, resolveWorkspace = target => configuredOwnerWorkspace(target, { profileRoot, python, commandPrefix, execute }) } = {}) {
  const active = new Map()
  let stopping = false
  const resolveTask = (task, resolveStoredSession = null, technicalOwner) => {
    if (task.completion || task.recovery_id) return technicalOwnerIdentity(technicalOwner, { resolveWorkspace })
    if (technicalOwner !== undefined) throw new Error('ace_handoff_technical_owner_requires_completion')
    const profile = task.owner_process?.profile
    validateOwnerProfile(profile, ownerProfiles)
    const session = magnusBroker && profile === 'magnus'
      ? brokerResolve(task.context_id)
      : resolveStoredSession
        ? resolveStoredSession(profile, task.context_id, task)
        : resolveSession({ profile, ownerProfiles, contextId: task.context_id, profileRoot, task })
    return { target: profile, ...session, cwd: validateWorkspace(session.cwd) }
  }
  return {
    readTerminalIncident: task => readTerminalIncident(task, { profileRoot, incidentRoot }),
    async stop () {
      stopping = true
      await Promise.all([...active.values()].map(async ({ child, closed }) => {
        child.kill('SIGTERM')
        const timer = setTimeout(() => child.kill('SIGKILL'), 10_000)
        try { await closed } catch {} finally { clearTimeout(timer) }
      }))
    },
    resolve: (task, technicalOwner) => resolveTask(task, null, technicalOwner),
    readReport (task, execution) {
      try { return readOwnerPublicReport({ profileRoot, ownerProfiles, target: execution.target, sessionId: execution.session_id, marker: handoffMarker(task, execution.handoff_id), recordedAt: execution.recorded_at }) } catch (error) {
        if (['ENOENT', 'EACCES', 'SQLITE_CANTOPEN'].includes(error.code)) return { status: 'unavailable', reason: 'native_public_source_unavailable' }
        throw error
      }
    },
    resolveContinuation (task, technicalOwner) {
      const previous = task.handoff
      const allowedTarget = task.completion || task.recovery_id ? TECHNICAL_OWNERS.has(previous?.target) : Object.values(ownerProfiles).includes(previous?.target)
      if (!previous || !allowedTarget || previous.status === 'running') throw new Error('ace_handoff_continuation_invalid')
      if (technicalOwner !== undefined) {
        if (!task.completion && !task.recovery_id) throw new Error('ace_handoff_technical_owner_requires_completion')
        validateTechnicalOwner(technicalOwner)
        if (technicalOwner !== previous.target) throw new Error('ace_handoff_continuation_owner_conflict')
      }
      if (task.completion || task.recovery_id) return technicalHandoffIdentity(profileRoot, previous.target, previous.session_id, handoffMarker(task, previous.handoff_id))
      const sessionId = previous.session_id || completedHandoffSession(profileRoot, previous.target, handoffMarker(task, previous.handoff_id))
      if (!sessionId) throw new Error('ace_owner_session_unresolved')
      const identity = resolveTask(task)
      if (identity.target !== previous.target || identity.session_id !== sessionId) throw new Error('ace_owner_identity_invalid')
      return identity
    },
    createResolver () {
      const sessions = new Map()
      const resolveStoredSession = (profile, contextId, task) => {
        if (!sessions.has(profile)) sessions.set(profile, createSessionResolver({ profile, ownerProfiles, profileRoot }))
        return sessions.get(profile).resolve(contextId, task)
      }
      return Object.freeze({
        resolve: task => resolveTask(task, resolveStoredSession),
        close () {
          for (const resolver of sessions.values()) resolver.close()
          sessions.clear()
        }
      })
    },
    async run ({ task, target, session_id: sessionId, cwd, directory, taskRef, moduleRoot, handoffId, nextAction, evidenceRefs }) {
      if (stopping) throw new Error('ace_owner_runner_stopping')
      if (task.completion || task.recovery_id) validateTechnicalOwner(target)
      else validateOwnerProfile(target, ownerProfiles)
      validateWorkspace(cwd)
      mkdirSync(directory, { recursive: true })
      const prompt = task.recovery_id
        ? `Technical recovery for existing PB proposal ${task.recovery_id}. This is maintenance, not a new ACE business task or a replacement proposal. Its original source, author, request and input are immutable: ${JSON.stringify(task.original)}. Read the recovery record at ${taskRef} and its original_job_ref when present. Inspect and repair only the operational cause within the selected ${target} profile's existing mandate; another repo needs its actual owner's authority. Do not invent source, change the original fact or replay domain actions. After repair Pax will recover the exact recorded proposal and independently verify native curation history and facts. An unavailable source/capability/authority requires a concrete blocker and next action, not a wider grant.`
        : task.completion
        ? `Operational recovery for existing PB ACE task ${task.context_id}. A saved original completion could not be processed. Inspect the recorded failure and resolve the operational cause within existing authority; do not invent or change the original outcome, replay domain actions, or send external messages. Preserve a local actionable report if repair needs unavailable authority. Read the original task/checkpoint at ${taskRef}; its runtime module is ${moduleRoot}. Existing task evidence: ${JSON.stringify({ context_id: task.context_id, binding: task.binding, last_failure: task.last_failure })}. This is maintenance, not a new ACE business task. Pax will verify repair by replaying the exact saved PB completion once after you finish.`
        : `Continue the original interrupted task in this same conversation. Its existing ACE context_id is ${task.context_id}. Keep its original session_id and task_id from the existing context call. Finish only the originally authorized work and submit its actual outcome with ace_learn; do not create a replacement context or invent missing evidence. Do not repeat domain actions already recorded as completed. If a needed decision or authority is missing, report the blocker locally. Do not send external messages.`
      const promptPath = join(directory, 'request.txt')
      const outputPath = join(directory, 'result.txt')
      const marker = handoffMarker(task, handoffId)
      const maintenance = task.completion || task.recovery_id ? `\nRepair the cause using the existing repo worktree, targeted test, review and release routes within this owner's current mandate. Selected technical owner: ${target}. Read and preserve this profile's existing ownership and authority boundaries. This selection does not add cross-repo or deployment authority. A local commit is not activation or recovery. Record the tested SHA, actual affected runtime/module binding and release/readback evidence when activation is authorized. If source, capability or authority is unavailable, report the exact blocker and concrete next action; do not invent a broader grant. Continue longer work in this same native session with its checkpoints. Do not restart your own running gateway.` : ''
      const continuation = task.handoff ? `\nThis continues the same existing recovery handoff ${task.handoff.handoff_id}; preserve its technical work and originally authorized scope. Prior execution evidence: ${task.handoff.receipt_ref || 'unavailable'}. Requested next action and new evidence: ${JSON.stringify({ next_action: nextAction, evidence_refs: evidenceRefs })}.` : ''
      const request = `${marker}\n\n${prompt}${maintenance}${continuation}`
      writeFileSync(promptPath, request, { mode: 0o600 })
      const output = openSync(outputPath, 'a', 0o600)
      const args = [...commandPrefix, ...(sessionId ? ['--resume', sessionId] : []), 'chat', '--query-file', promptPath, '--in', cwd, '--max-turns', '20', '--run-budget', '540', '-Q']
      const brokered = magnusBroker && target === 'magnus'
      const child = brokered
        ? spawnProcess('/usr/bin/sudo', ['-n', '/usr/local/bin/buzz-agent-broker', 'ace-owner-resume'], { env: { PATH: '/usr/bin:/bin' }, stdio: ['pipe', output, 'ignore'] })
        : spawnProcess(python, args, { env: ownerProcessEnv(profileRoot, target), stdio: ['ignore', output, output] })
      if (brokered) {
        // A denied/exited broker may close stdin; its exit/result remains authoritative.
        child.stdin.on('error', () => {})
        child.stdin.end(JSON.stringify({ context_id: task.context_id, handoff_id: handoffId }))
      }
      closeSync(output)
      const closed = new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve) })
      active.set(child, { child, closed })
      let timedOut = false
      let killTimer
      const timer = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); killTimer = setTimeout(() => child.kill('SIGKILL'), 10_000) }, timeoutMs)
      const code = await closed.finally(() => { active.delete(child); clearTimeout(timer); clearTimeout(killTimer) })
      let nativeOutput = readFileSync(outputPath, 'utf8')
      let nativeCode = code
      if (brokered && !timedOut) {
        let result
        try { result = JSON.parse(nativeOutput) } catch { throw new Error('ace_owner_broker_result_invalid') }
        if (result.session_id !== sessionId || result.context_id !== task.context_id || result.handoff_id !== handoffId || typeof result.output !== 'string') throw new Error('ace_owner_broker_result_invalid')
        nativeOutput = result.output
        nativeCode = result.exit_code
        timedOut = result.timed_out === true
      }
      const nativeSession = sessionId || completedHandoffSession(profileRoot, target, marker)
      const receiptPath = join(dirname(taskRef), `handoff-${handoffId}.json`)
      const receiptTemporary = `${receiptPath}.tmp-${process.pid}`
      writeFileSync(receiptTemporary, `${JSON.stringify({ schema_version: task.recovery_id ? 'oos.proposal_owner_execution.v1' : 'oos.ace_owner_execution.v1', handoff_id: handoffId, ...(task.recovery_id ? { recovery_id: task.recovery_id } : { context_id: task.context_id }), target, session_id: nativeSession, request, output: nativeOutput, exit_code: nativeCode, timed_out: timedOut, recorded_at: new Date().toISOString() }, null, 2)}\n`, { mode: 0o600, flag: 'wx' })
      renameSync(receiptTemporary, receiptPath)
      return { exit_code: nativeCode, timed_out: timedOut, receipt_ref: receiptPath, target, session_id: nativeSession }
    }
  }
}
