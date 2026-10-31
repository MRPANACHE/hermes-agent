// Synthetic E2E binding only. Product binding.mjs and sealed consumer stay unchanged.
import * as fs from 'node:fs/promises'
import { join } from 'node:path'
import Database from './consumer/node_modules/better-sqlite3/lib/index.js'
import { sha256 } from './consumer/src/episode-ledger/hash.js'
import { GBRAIN_CODEX_SUBSCRIPTION_MODEL } from './consumer/src/transport/gbrain-codex-subscription-provenance.js'
import { createGbrainBridgeAuditLedger } from './consumer/src/transport/gbrain-bridge-audit.js'
import { createGbrainCodexSubscriptionQueue } from './consumer/src/transport/gbrain-codex-subscription-queue.js'
import { createPaxNativeReadChannel, recoveryId, nativeSession } from '../../../integrations/pax-recovery-native/channel.mjs'

export async function reconcileFixedPaxRecovery (socket) {
  const home = process.env.HOME
  const scenario = await fs.readFile(join(home, 'scenario.txt'), 'utf8')
  const root = await fs.mkdtemp(join(home, 'synthetic-consumer-'))
  const stateDir = join(root, 'queue'), profileRoot = join(root, 'profiles')
  await fs.mkdir(join(stateDir, 'jobs'), { recursive: true, mode: 0o700 })
  await fs.mkdir(join(profileRoot, 'pax'), { recursive: true, mode: 0o700 })
  const source = `codex-conversations/${'b'.repeat(32)}/${'c'.repeat(32)}`
  const id = recoveryId, session = nativeSession
  const oldTime = '2026-10-27T20:00:00.000Z', newTime = '2026-10-30T17:00:00.000Z'
  const job = { schema: 'oos.gbrain_codex_subscription_job.v1', candidates: [], original_turn: 'Synthetic source.', source_ref: source,
    session_hash: 'b'.repeat(32), turn_hash: 'c'.repeat(32), origin: 'human', submitter: 'human:synthetic',
    job_id: `job-${id}`, input_hash: id, request_id: `pax-curation:${id}`, model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
    candidate_hashes: [], status: 'failed', attempts: 1, next_attempt_at: oldTime, created_at: oldTime,
    updated_at: oldTime, lease: null, facts: [], remembered: [], remember_intents: [], fact_refs: [], audit_refs: [],
    last_error: 'pax_memory_curation_result_invalid', completed_at: oldTime }
  const path = join(stateDir, 'jobs', `${job.job_id}.json`)
  const raw = `${JSON.stringify(job, null, 2)}\n\n`
  await fs.writeFile(path, raw, { mode: 0o600 })
  const db = new Database(join(profileRoot, 'pax', 'state.db'))
  db.exec(`CREATE TABLE sessions(id TEXT,profile_name TEXT,source TEXT,parent_session_id TEXT);
    CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_name TEXT,tool_call_id TEXT);`)
  const request = { schema: 'pb.pax_memory_curation.v1', request_id: job.request_id, run_id: job.request_id,
    source_ref: source, origin: job.origin, submitter: job.submitter, source: job.original_turn, source_hash: sha256(job.original_turn) }
  const newFact = { fact: 'Synthetic fact.', entity_slug: 'example', kind: 'fact', visibility: 'world' }
  const decision = { action: 'add', old_fact_ids: [], new_fact_id: 41, reason: 'Synthetic reason.', source_refs: [source], visibility: 'world' }
  const args = { request_id: job.request_id, pax_run: job.request_id, source_refs: [source], reason: 'Synthetic decision.',
    decisions: [{ action: 'add', old_fact_ids: [], reason: decision.reason, source_refs: [source], new_fact: newFact }] }
  const receipt = { request_id: job.request_id, replay: false, decisions: [decision] }
  const history = { request_id: job.request_id, pax_run: job.request_id, source_id: 'business-pb',
    curator_client_id: 'gbrain_cl_208108dbcf6e7d84a7c1d2cffebf74606d23f086467acafbcb62370030152287', created_at: oldTime, decisions: [decision] }
  const fact = { id: 41, ...newFact, active: true, source_id: 'business-pb', source_session: job.request_id }
  db.prepare('INSERT INTO sessions VALUES(?,?,?,NULL)').run(session, 'pax', 'cli')
  const insert = db.prepare('INSERT INTO messages VALUES(?,?,?,?,?,?,?)')
  insert.run(1, session, 'user', `PAX_MEMORY_CURATION_V1: synthetic\nUNTRUSTED REQUEST DATA: ${JSON.stringify(request)}`, null, null, null)
  insert.run(2, session, 'assistant', null, JSON.stringify([{ id: 'native-call', function: { name: 'tool_call', arguments: JSON.stringify({ name: 'mcp__brain__curate_facts', arguments: args }) } }]), null, null)
  insert.run(3, session, 'tool', `<untrusted_tool_result source="mcp__brain__curate_facts">\nSynthetic framing.\n\n${JSON.stringify({ result: JSON.stringify(receipt) })}\n</untrusted_tool_result>`, null, 'mcp__brain__curate_facts', 'native-call')
  insert.run(4, session, 'assistant', JSON.stringify(receipt), null, null, null)
  const ledgerDir = join(root, 'ledger')
  await fs.mkdir(ledgerDir, { mode: 0o700 })
  const ledger = createGbrainBridgeAuditLedger({ stateDir: ledgerDir, now: () => newTime })
  const channel = createPaxNativeReadChannel(socket)
  const fsImpl = { ...fs, rename: async (from, to) => {
    if (to === path && scenario === 'before-jobwrite') throw new Error('synthetic before jobwrite')
    await fs.rename(from, to)
    if (to === path && scenario === 'after-jobwrite') throw new Error('synthetic after jobwrite')
  } }
  const queue = createGbrainCodexSubscriptionQueue({ stateDir, now: () => newTime, fsImpl,
    auditLedger: { read: () => ledger.read(), record: async event => {
      if (scenario === 'before-audit') throw new Error('synthetic before audit')
      const result = await ledger.record(event)
      if (scenario === 'changed-raw') await fs.writeFile(path, JSON.stringify(job) + '\n')
      if (scenario === 'after-audit') throw new Error('synthetic after audit')
      return result
    } }, historicalReceipt: { profileRoot, callVerb: channel.callVerb } })
  if (scenario === 'conflict') {
    await fs.mkdir(join(stateDir, 'historical-receipts', 'proofs'), { recursive: true })
    await fs.writeFile(join(stateDir, 'historical-receipts', 'proofs', `${id}.json`), '{}')
  }
  await fs.writeFile(join(home, 'fixture-ready.json'), JSON.stringify({ root, path, stateDir, ledgerDir, history, fact, raw }))
  try {
    const result = await queue.reconcileHistoricalReceipt({ recovery_id: id, native_session_id: session })
    return { ok: true, result, job: JSON.parse(await fs.readFile(path, 'utf8')), events: (await ledger.read()).events.length }
  } catch (error) {
    return { ok: false, error: error.message, job: JSON.parse(await fs.readFile(path, 'utf8')), events: (await ledger.read()).events.length }
  } finally {
    channel.close()
    db.close()
  }
}
