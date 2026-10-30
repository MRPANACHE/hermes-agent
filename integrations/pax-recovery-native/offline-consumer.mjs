import test from 'node:test'
import assert from 'node:assert/strict'
import * as fs from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import Database from 'better-sqlite3'
import { canonicalJson, sha256 } from '../../../consumer/src/episode-ledger/hash.js'
import { GBRAIN_CODEX_SUBSCRIPTION_MODEL, gbrainCodexSubscriptionInputHash } from '../../../consumer/src/transport/gbrain-codex-subscription-provenance.js'
import { createGbrainBridgeAuditLedger } from '../../../consumer/src/transport/gbrain-bridge-audit.js'
import { GBRAIN_CODEX_SUBSCRIPTION_KILL_SWITCH } from '../../../consumer/src/transport/gbrain-codex-subscription-extractor.js'
import { createGbrainCodexSubscriptionQueue } from '../../../consumer/src/transport/gbrain-codex-subscription-queue.js'

import net from 'node:net'
import { createPaxNativeReadChannel, recoveryId, nativeSession } from './channel.mjs'
const channel = createPaxNativeReadChannel(new net.Socket({fd: Number(process.argv[2]), readable:true, writable:true}))
async function fixture (t) {
  const root = await fs.mkdtemp(join(tmpdir(), 'historical-consumer-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  const stateDir = join(root, 'queue'), profileRoot = join(root, 'profiles')
  await fs.mkdir(join(stateDir, 'jobs'), { recursive: true, mode: 0o700 })
  await fs.mkdir(join(profileRoot, 'pax'), { recursive: true, mode: 0o700 })
  const source = `codex-conversations/${'b'.repeat(32)}/${'c'.repeat(32)}`
  const input = { candidates: [], original_turn: 'Synthetic source.', source_ref: source,
    session_hash: 'b'.repeat(32), turn_hash: 'c'.repeat(32), origin: 'human', submitter: 'human:synthetic' }
  const id = recoveryId, session = nativeSession
  const oldTime = '2026-10-27T20:00:00.000Z', newTime = '2026-10-30T17:00:00.000Z'
  const job = { schema: 'oos.gbrain_codex_subscription_job.v1', ...input, job_id: `job-${id}`, input_hash: id,
    request_id: `pax-curation:${id}`, model: GBRAIN_CODEX_SUBSCRIPTION_MODEL, candidate_hashes: [], status: 'failed',
    attempts: 1, next_attempt_at: oldTime, created_at: oldTime, updated_at: oldTime, lease: null,
    facts: [], remembered: [], remember_intents: [], fact_refs: [], audit_refs: [],
    last_error: 'pax_memory_curation_result_invalid', completed_at: oldTime }
  const path = join(stateDir, 'jobs', `${job.job_id}.json`)
  const raw = `${JSON.stringify(job, null, 2)}\n\n`
  await fs.writeFile(path, raw, { mode: 0o600 })
  const db = new Database(join(profileRoot, 'pax', 'state.db'))
  t.after(() => db.close())
  db.exec(`CREATE TABLE sessions(id TEXT,profile_name TEXT,source TEXT,parent_session_id TEXT);
    CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_name TEXT,tool_call_id TEXT);`)
  const request = { schema: 'pb.pax_memory_curation.v1', request_id: job.request_id, run_id: job.request_id,
    source_ref: source, origin: job.origin, submitter: job.submitter, source: job.original_turn, source_hash: sha256(job.original_turn) }
  const newFact = { fact: 'Synthetic fact.', entity_slug: 'example', kind: 'fact', visibility: 'world' }
  const decision = { action: 'add', old_fact_ids: [], new_fact_id: 41, reason: 'Synthetic reason.', source_refs: [source], visibility: 'world' }
  const args = { request_id: job.request_id, pax_run: job.request_id, source_refs: [source], reason: 'Synthetic decision.',
    decisions: [{ action: 'add', old_fact_ids: [], reason: decision.reason, source_refs: [source], new_fact: newFact }] }
  const receipt = { request_id: job.request_id, replay: false, decisions: [decision] }
  const history = { request_id: job.request_id, pax_run: job.request_id, source_id: 'business-pb', curator_client_id: 'gbrain_cl_208108dbcf6e7d84a7c1d2cffebf74606d23f086467acafbcb62370030152287', created_at: oldTime, decisions: [decision] }
  const fact = { id: 41, ...newFact, active: true, source_id: 'business-pb', source_session: job.request_id }
  db.prepare('INSERT INTO sessions VALUES(?,?,?,NULL)').run(session, 'pax', 'cli')
  const insert = db.prepare('INSERT INTO messages VALUES(?,?,?,?,?,?,?)')
  insert.run(1, session, 'user', `PAX_MEMORY_CURATION_V1: synthetic\nUNTRUSTED REQUEST DATA: ${JSON.stringify(request)}`, null, null, null)
  insert.run(2, session, 'assistant', null, JSON.stringify([{ id: 'native-call', function: { name: 'tool_call', arguments: JSON.stringify({ name: 'mcp__brain__curate_facts', arguments: args }) } }]), null, null)
  insert.run(3, session, 'tool', `<untrusted_tool_result source="mcp__brain__curate_facts">\nSynthetic framing.\n\n${JSON.stringify({ result: JSON.stringify(receipt) })}\n</untrusted_tool_result>`, null, 'mcp__brain__curate_facts', 'native-call')
  insert.run(4, session, 'assistant', JSON.stringify(receipt), null, null, null)
  await fs.mkdir(join(root, 'ledger'), { mode: 0o700 })
  const ledger = createGbrainBridgeAuditLedger({ stateDir: join(root, 'ledger'), now: () => newTime })
  let onCall = async () => {}, onAudit = async () => {}, fault = '', calls = 0
  const fsImpl = { ...fs, rename: async (from, to) => {
    if (to === path && fault === 'before-jobwrite') throw new Error('synthetic crash before jobwrite')
    await fs.rename(from, to)
    if (to === path && fault === 'after-jobwrite') throw new Error('synthetic crash after jobwrite')
  } }
  const queue = createGbrainCodexSubscriptionQueue({ stateDir, now: () => newTime, fsImpl,
    auditLedger: { read: () => ledger.read(), record: async event => {
      if (fault === 'before-audit') throw new Error('synthetic crash before audit')
      const result = await ledger.record(event)
      await onAudit()
      if (fault === 'after-audit') throw new Error('synthetic crash after audit')
      return result
    } }, historicalReceipt: { profileRoot, callVerb: channel.callVerb } })
  const run = () => queue.reconcileHistoricalReceipt({ recovery_id: id, native_session_id: session })
  return { root, stateDir, profileRoot, job, raw, path, id, session, db, fact, history, queue, ledger, run,
    setFault: value => { fault = value }, onCall: value => { onCall = value }, onAudit: value => { onAudit = value },
    read: async () => JSON.parse(await fs.readFile(path, 'utf8')),
    write: value => fs.writeFile(path, JSON.stringify(value), { mode: 0o600 }),
    evidence: (kind) => join(stateDir, 'historical-receipts', kind, `${id}.json`) }
}

const cleanup=[]
const f = await fixture({after: fn => cleanup.push(fn)})
const scenario = process.argv[3]
if (scenario === 'changed-job') {
  const original=channel.callVerb
  // Fault is applied via the real audit hook, not by replacing the native reader.
  f.onAudit(async () => fs.writeFile(f.path, JSON.stringify({...f.job, updated_at:'2026-10-30T16:00:00.000Z'})))
}
if (scenario === 'conflict') {
  await fs.mkdir(join(f.stateDir, 'historical-receipts', 'proofs'), {recursive:true, mode:0o700})
  await fs.writeFile(f.evidence('proofs'), '{}', {mode:0o600})
}
if (scenario === 'linked-conflict') {
  await fs.mkdir(join(f.stateDir, 'recovery'), {recursive:true, mode:0o700})
  await fs.writeFile(join(f.stateDir, 'recovery', `${f.id}.json`), JSON.stringify({handoff:{status:'running'}}), {mode:0o600})
}
if (scenario === 'changed-raw') f.onAudit(async () => fs.writeFile(f.path, JSON.stringify(f.job)+'\n'))
console.log(JSON.stringify({ready:true, history:f.history, fact:f.fact}))
try {
  const result = await f.run()
  const job=await f.read()
  const events=(await f.ledger.read()).events
  console.log(JSON.stringify({ok:true,result,job,events:events.length}))
} catch (error) {
  console.log(JSON.stringify({ok:false,error:error.message,job:await f.read(),events:(await f.ledger.read()).events.length}))
} finally {
  channel.close()
  for (const fn of cleanup.reverse()) await fn()
}
