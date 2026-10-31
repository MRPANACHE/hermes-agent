import { constants } from 'node:fs'
import * as fs from 'node:fs/promises'
import { isAbsolute, join } from 'node:path'
import { randomUUID } from 'node:crypto'
import Database from 'better-sqlite3'
import { canonicalJson, sha256 } from '../episode-ledger/hash.js'

const TOOL = 'mcp__brain__curate_facts'
const MARKER = '\nUNTRUSTED REQUEST DATA: '
const SEMANTIC_KEYS = ['action', 'old_fact_ids', 'new_fact_id', 'visibility', 'reason', 'source_refs']
const fail = reason => { throw new Error(`pax_historical_receipt_${reason}`) }
const same = (a, b) => canonicalJson(a) === canonicalJson(b)
const present = value => typeof value === 'string' && value.length > 0
const positiveId = value => Number.isSafeInteger(value) && value > 0
function parse (text) {
  if (typeof text !== 'string' || Buffer.byteLength(text) > 256 * 1024) fail('native_json_invalid')
  try { return JSON.parse(text) } catch { fail('native_json_invalid') }
}
function semantic (decision) {
  if (!decision || SEMANTIC_KEYS.some(key => !Object.hasOwn(decision, key))) fail('decision_incomplete')
  return Object.fromEntries(SEMANTIC_KEYS.map(key => [key, decision[key]]))
}
function eligible (job, id) {
  if (job?.job_id !== `job-${id}` || job.input_hash !== id || job.request_id !== `pax-curation:${id}` ||
      job.status !== 'failed' || job.last_error !== 'pax_memory_curation_result_invalid' ||
      Object.hasOwn(job, 'curation_runtime') || Object.hasOwn(job, 'recovery_of') ||
      job.origin !== 'human' || !present(job.submitter) || !present(job.original_turn) ||
      !/^codex-conversations\/[a-f0-9]{32}\/[a-f0-9]{32}$/u.test(job.source_ref)) fail('job_ineligible')
}

function nativeEvidence (db, job, sessionId) {
  db.pragma('query_only = ON')
  return db.transaction(() => {
    const session = db.prepare('SELECT id,profile_name,source,parent_session_id FROM sessions WHERE id=?').get(sessionId)
    // No inferred identity from a copied transcript, compressed lineage, or another profile.
    if (!session || session.profile_name !== 'pax' || session.source !== 'cli' || session.parent_session_id) fail('native_identity_unverified')
    const users = db.prepare("SELECT id,content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 2").all(sessionId)
    if (users.length !== 1 || !users[0].content?.startsWith('PAX_MEMORY_CURATION_V1:')) fail('native_input_ambiguous')
    const at = users[0].content.indexOf(MARKER)
    if (at < 0) fail('native_input_missing')
    const request = parse(users[0].content.slice(at + MARKER.length))
    if (request.schema !== 'pb.pax_memory_curation.v1' || request.request_id !== job.request_id || request.run_id !== job.request_id ||
        request.source_ref !== job.source_ref || request.origin !== job.origin || request.submitter !== job.submitter ||
        request.source !== job.original_turn || request.source_hash !== sha256(job.original_turn) ||
        !same(request.prior_context ?? null, job.prior_context ?? null)) fail('native_source_mismatch')
    const rows = db.prepare("SELECT id,tool_calls FROM messages WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL ORDER BY id LIMIT 101").all(sessionId)
    if (rows.length > 100) fail('native_calls_truncated')
    const calls = []
    for (const row of rows) {
      const entries = parse(row.tool_calls)
      if (!Array.isArray(entries)) fail('native_calls_invalid')
      for (const call of entries) {
        const args = parse(call.function?.arguments)
        // The native gateway records the deferred invocation, not a model-supplied receipt.
        if (call.function?.name === 'tool_call' && args.name === TOOL) calls.push({ row, call, args: args.arguments })
      }
    }
    if (calls.length !== 1) fail('native_call_ambiguous')
    const { row, call, args } = calls[0]
    if (row.id <= users[0].id || !present(call.id) || args?.request_id !== job.request_id || args.pax_run !== job.request_id ||
        !same(args.source_refs, [job.source_ref]) || !present(args.reason) || !Array.isArray(args.decisions) ||
        args.decisions.length < 1 || args.decisions.length > 4) fail('native_call_mismatch')
    const tools = db.prepare("SELECT id,content FROM messages WHERE session_id=? AND role='tool' AND tool_name=? AND tool_call_id=? ORDER BY id LIMIT 2").all(sessionId, TOOL, call.id)
    if (tools.length !== 1 || tools[0].id <= row.id) fail('native_receipt_unbound')
    const wrapped = tools[0].content
    const header = `<untrusted_tool_result source="${TOOL}">\n`
    if (!wrapped?.startsWith(header) || !wrapped.endsWith('\n</untrusted_tool_result>')) fail('native_receipt_framing')
    const split = wrapped.indexOf('\n\n', header.length)
    if (split < 0) fail('native_receipt_framing')
    const envelope = parse(wrapped.slice(split + 2, -'\n</untrusted_tool_result>'.length))
    if (!envelope || Object.hasOwn(envelope, 'error')) fail('native_receipt_error')
    const receipt = parse(envelope.result)
    const final = db.prepare("SELECT id,role,content,tool_calls FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1").get(sessionId)
    if (final?.role !== 'assistant' || final.id <= tools[0].id || (final.tool_calls && final.tool_calls !== '[]') ||
        !same(parse(final.content), receipt) || receipt.request_id !== job.request_id || typeof receipt.replay !== 'boolean' ||
        !Array.isArray(receipt.decisions) || receipt.decisions.length !== args.decisions.length) fail('native_final_mismatch')
    return { args, receipt, proof: { profile: 'pax', session_id: sessionId, user_message_id: users[0].id,
      call_message_id: row.id, tool_call_id: call.id, receipt_message_id: tools[0].id, final_message_id: final.id,
      request_sha256: sha256(request), call_sha256: sha256(args), receipt_sha256: sha256(receipt) } }
  })()
}

export async function persistPaxHistoricalEvidence (auditDir, id, receipt) {
  if (!/^[a-f0-9]{64}$/u.test(id)) fail('identity_invalid')
  await fs.mkdir(auditDir, { mode: 0o700, recursive: true })
  const directoryHandle = await fs.open(auditDir, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW).catch(() => fail('audit_directory_unsafe'))
  try {
    const directory = await directoryHandle.stat()
    if ((directory.mode & 0o077) || directory.uid !== process.getuid()) fail('audit_directory_unsafe')
    // This native Linux primitive pins the checked directory across rename/symlink races.
    const pinned = `/proc/self/fd/${directoryHandle.fd}`
    const target = join(pinned, `${id}.json`)
    const temporary = join(pinned, `.${id}.${randomUUID()}.tmp`)
    const bytes = `${canonicalJson(receipt)}\n`
    const handle = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    try {
      try {
        await handle.writeFile(bytes, 'utf8')
        await handle.sync()
      } finally { await handle.close() }
      try { await fs.link(temporary, target) } catch (error) { if (error.code !== 'EEXIST') throw error }
      const stored = await fs.open(target, constants.O_RDONLY | constants.O_NOFOLLOW).catch(() => fail('audit_target_unsafe'))
      try {
        const info = await stored.stat()
        if (!info.isFile() || info.size > 256 * 1024 || info.uid !== process.getuid() || (info.mode & 0o077) ||
            await stored.readFile('utf8') !== bytes) fail('audit_conflict')
      } finally { await stored.close() }
      await directoryHandle.sync()
      const current = await fs.lstat(auditDir)
      if (!current.isDirectory() || current.dev !== directory.dev || current.ino !== directory.ino) fail('audit_directory_changed')
      return { status: 'receipt_verified_only', receipt_ref: join(auditDir, `${id}.json`), receipt_sha256: sha256(bytes), receipt }
    } finally { await fs.unlink(temporary) }
  } finally { await directoryHandle.close() }
}

/** Draft-only receipt reconciliation. No caller evidence payload, fact write, job write, or execution attestation. */
export function createPaxHistoricalReceiptReconciler ({ readJob, callVerb, auditDir,
  profileRoot = '/home/david/.hermes/profiles',
  openDatabase = path => new Database(path, { readonly: true, fileMustExist: true }) } = {}) {
  if (typeof readJob !== 'function' || typeof callVerb !== 'function' || typeof openDatabase !== 'function' ||
      process.platform !== 'linux' || typeof auditDir !== 'string' || !isAbsolute(auditDir) || typeof profileRoot !== 'string' || !isAbsolute(profileRoot)) fail('configuration_invalid')
  return Object.freeze({ async reconcile (input) {
    if (!input || !same(Object.keys(input).sort(), ['native_session_id', 'recovery_id']) ||
        !/^[a-f0-9]{64}$/u.test(input.recovery_id) || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$/u.test(input.native_session_id)) fail('identity_invalid')
    const { recovery_id: id, native_session_id: sessionId } = input
    const job = structuredClone(await readJob(`job-${id}`))
    eligible(job, id)
    let db, native
    try {
      db = openDatabase(join(profileRoot, 'pax', 'state.db'))
      native = nativeEvidence(db, job, sessionId)
    } catch (error) {
      if (error.message?.startsWith('pax_historical_receipt_')) throw error
      fail('native_unavailable')
    } finally { db?.close() }
    const response = await callVerb('curation_history', { request_id: job.request_id, limit: 100 })
    if (!response || response.next_cursor !== null || !Array.isArray(response.items) || response.items.length !== 1) fail('history_incomplete_or_ambiguous')
    const history = response.items[0]
    if (history.request_id !== job.request_id || history.pax_run !== job.request_id || history.source_id !== 'business-pb' ||
        !present(history.curator_client_id) || !Number.isFinite(Date.parse(history.created_at)) || !Array.isArray(history.decisions) ||
        !same(history.decisions.map(semantic), native.receipt.decisions.map(semantic))) fail('history_identity_mismatch')
    const facts = [], refs = []
    for (const [index, decision] of history.decisions.entries()) {
      const attempted = native.args.decisions[index]
      // This preparatory contract covers lost add receipts for original human jobs, not historical replacement/retraction authority.
      if (decision.action !== 'add' || !same(decision.old_fact_ids, []) || !positiveId(decision.new_fact_id) || decision.visibility !== 'world' ||
          !present(decision.reason) || !Array.isArray(decision.source_refs) || !decision.source_refs.includes(job.source_ref) ||
          !decision.source_refs.every(present) || attempted.action !== 'add' || !same(attempted.old_fact_ids, []) ||
          attempted.reason !== decision.reason || !same(attempted.source_refs, decision.source_refs) ||
          !attempted.new_fact || !present(attempted.new_fact.fact) || !present(attempted.new_fact.entity_slug) ||
          !['event', 'preference', 'commitment', 'belief', 'fact'].includes(attempted.new_fact.kind) || attempted.new_fact.visibility !== 'world') fail('decision_unverified')
      const readback = await callVerb('list_facts', { after_id: decision.new_fact_id - 1, limit: 1 })
      const fact = readback?.facts?.[0]
      if (readback?.facts?.length !== 1 || fact.id !== decision.new_fact_id || fact.active !== true || fact.source_id !== history.source_id ||
          fact.source_session !== job.request_id || ['fact', 'entity_slug', 'kind', 'visibility'].some(key => fact[key] !== attempted.new_fact[key])) fail('fact_unverified')
      refs.push(`gbrain-fact:${fact.id}`)
      facts.push({ id: fact.id, sha256: sha256(fact) })
    }
    if (new Set(refs).size !== refs.length) fail('duplicate_fact_reference')
    // A current source change invalidates the proof; never silently attest an earlier projection.
    if (!same(await readJob(job.job_id), job)) fail('job_changed')
    const receipt = { schema: 'pb.pax_historical_receipt.v1', disposition: 'receipt_verified_only', recovery_id: id,
      job_id: job.job_id, job_sha256: sha256(job), request_id: job.request_id, source_ref: job.source_ref,
      source_sha256: sha256(job.original_turn), source_id: history.source_id, native: native.proof,
      history_sha256: sha256(history), facts, fact_refs: refs,
      historical_runtime: { status: 'unknown', reason: 'historical_runtime_not_recorded' } }
    return persistPaxHistoricalEvidence(auditDir, id, receipt)
  } })
}
