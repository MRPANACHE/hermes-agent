import { constants } from 'node:fs'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { canonicalSha256 } from './vnext/schemas/agent-mandate.v1.js'

export const GBRAIN_BRIDGE_AUDIT_SCHEMA = 'oos.gbrain_bridge_operation.v1'
export const DEFAULT_GBRAIN_BRIDGE_AUDIT_STATE_DIR = '/home/david/.local/state/operator-os/gbrain-codex-chat'
export const DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_FILE_BYTES = 256 * 1024
export const DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_SEGMENTS = 8

const SEGMENT_PATTERN = /^events-(\d{6})\.jsonl$/u
const SAFE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/u
const SAFE_REF = /^(?:codex-session:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|codex-turn:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|codex-correlation:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|codex-conversations\/[a-f0-9]{32}\/[a-f0-9]{32}|codex-chats\/[a-f0-9]{32}\/[a-f0-9]{32}-(?:user|assistant)|gbrain-fact:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-extraction:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-provenance:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-start:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-recall:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-application:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-write-audit:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-collection:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|gbrain-readback:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|local-[a-z][a-z0-9-]{0,48}:[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$/u
const SHA256 = /^[a-f0-9]{64}$/u
const PHASES = new Set(['attempted', 'completed', 'failed'])
const OPERATIONS = new Set(['archive', 'put_page', 'get_page', 'extract_facts', 'codex_subscription_enqueue', 'codex_subscription_extract', 'codex_subscription_curate', 'remember', 'recall', 'service_start', 'fact_readback', 'application', 'write_audit'])
const EVENT_INPUT_FIELDS = Object.freeze([
  'event_id', 'idempotency_key', 'operation', 'transport_verb', 'phase', 'session_ref',
  'turn_ref', 'correlation_ref', 'source_ref', 'result_ref', 'result_fact_refs',
  'result_count', 'error_code', 'timestamp'
])
const EVENT_FIELDS = Object.freeze([
  'schema', 'event_id', 'idempotency_key', 'operation', 'transport_verb', 'phase',
  'session_ref', 'turn_ref', 'correlation_ref', 'source_ref', 'result_ref',
  'result_fact_refs', 'result_count', 'error_code', 'timestamp', 'previous_hash',
  'local_event_hash'
])
const AUDIT_LOCK_TTL_MS = 30_000
const AUDIT_LOCK_WAIT_MS = 5_000
const DIRECTORY_OPEN_FLAGS = constants.O_RDONLY | (typeof constants.O_DIRECTORY === 'number' ? constants.O_DIRECTORY : 0) | (constants.O_NOFOLLOW || 0)

// Eén proces kan meerdere bridge-instanties hebben; serialiseer ze per state-dir.
const QUEUES = new Map()

function errorFor (code) {
  return Object.assign(new Error(code), { code })
}

function record (value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function sameLockObject (left, right) {
  return left?.dev !== undefined && left?.ino !== undefined && left.dev === right?.dev && left.ino === right?.ino && left.size === right?.size && left.mtimeMs === right?.mtimeMs
}

function safeText (value, field, max = 256) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max || /[\0\r\n]/u.test(value)) throw errorFor(`gbrain_audit_${field}_invalid`)
  return value
}

function safeNullableText (value, field, max = 256) {
  return value === null || value === undefined ? null : safeText(value, field, max)
}

function safeRef (value, field) {
  if (value === null || value === undefined) return null
  const text = safeText(value, field)
  if (!SAFE_REF.test(text)) throw errorFor(`gbrain_audit_${field}_invalid`)
  return text
}

function safeToken (value, field, max = 256) {
  if (value === null || value === undefined) return null
  const text = safeText(value, field, max)
  if (!SAFE_TOKEN.test(text)) throw errorFor(`gbrain_audit_${field}_invalid`)
  return text
}

function safeTimestamp (value) {
  const timestamp = value === undefined || value === null
    ? new Date().toISOString()
    : typeof value === 'number' && Number.isFinite(value) ? new Date(value).toISOString() : safeText(value, 'timestamp', 32)
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/u.test(timestamp) || !Number.isFinite(Date.parse(timestamp)) || new Date(timestamp).toISOString() !== timestamp) throw errorFor('gbrain_audit_timestamp_invalid')
  return timestamp
}

function safeResultFacts (value) {
  if (value === undefined || value === null) return []
  if (!Array.isArray(value) || value.length > 32) throw errorFor('gbrain_audit_result_fact_refs_invalid')
  const result = []
  for (const item of value) {
    const ref = safeRef(item, 'result_fact_ref')
    if (!ref.startsWith('gbrain-fact:') || result.includes(ref)) throw errorFor('gbrain_audit_result_fact_refs_invalid')
    result.push(ref)
  }
  return result
}

function safeCount (value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 1_000_000) throw errorFor('gbrain_audit_result_count_invalid')
  return value
}

function assertEventKeys (value) {
  if (!record(value)) throw errorFor('gbrain_audit_event_invalid')
  const keys = new Set(Object.keys(value))
  for (const key of keys) if (!EVENT_INPUT_FIELDS.includes(key)) throw errorFor('gbrain_audit_event_unknown_field')
}

function normalizeInputEvent (value, { now = () => new Date().toISOString(), eventIdFactory } = {}) {
  assertEventKeys(value)
  const eventId = safeToken(value.event_id === undefined ? eventIdFactory() : value.event_id, 'event_id')
  const operation = safeToken(value.operation, 'operation')
  if (!OPERATIONS.has(operation)) throw errorFor('gbrain_audit_operation_invalid')
  const transportVerb = safeToken(value.transport_verb === undefined ? operation : value.transport_verb, 'transport_verb')
  if (!PHASES.has(value.phase)) throw errorFor('gbrain_audit_phase_invalid')
  const resultRef = safeRef(value.result_ref, 'result_ref')
  const resultFactRefs = safeResultFacts(value.result_fact_refs)
  const resultCount = safeCount(value.result_count === undefined ? 0 : value.result_count)
  const errorCode = safeToken(value.error_code, 'error_code')
  const idempotencyKey = safeToken(value.idempotency_key, 'idempotency_key')
  const timestamp = value.timestamp === undefined ? safeTimestamp(now()) : safeTimestamp(value.timestamp)
  return {
    schema: GBRAIN_BRIDGE_AUDIT_SCHEMA,
    event_id: eventId,
    idempotency_key: idempotencyKey,
    operation,
    transport_verb: transportVerb,
    phase: value.phase,
    session_ref: safeRef(value.session_ref, 'session_ref'),
    turn_ref: safeRef(value.turn_ref, 'turn_ref'),
    correlation_ref: safeRef(value.correlation_ref, 'correlation_ref'),
    source_ref: safeRef(value.source_ref, 'source_ref'),
    result_ref: resultRef,
    result_fact_refs: resultFactRefs,
    result_count: resultCount,
    error_code: errorCode,
    timestamp
  }
}

function eventHash (value) {
  const { local_event_hash: _localEventHash, ...withoutHash } = value
  return canonicalSha256(withoutHash)
}

function sameIdempotentEvent (stored, normalized) {
  return EVENT_INPUT_FIELDS.filter((field) => field !== 'event_id' && field !== 'timestamp').every((field) => JSON.stringify(stored[field]) === JSON.stringify(normalized[field]))
}

function rememberTerminalIdentity (event) {
  if (event?.operation !== 'remember' || typeof event.idempotency_key !== 'string') return null
  if (event.phase === 'completed') return event.idempotency_key
  if (event.phase === 'failed' && event.idempotency_key.endsWith(':failed')) return event.idempotency_key.slice(0, -':failed'.length)
  return null
}

function assertRememberTerminalInvariant (events, candidate) {
  const identity = rememberTerminalIdentity(candidate)
  if (identity === null) return
  const opposite = events.find((event) => rememberTerminalIdentity(event) === identity && event.phase !== candidate.phase)
  if (opposite) throw errorFor('gbrain_audit_terminal_conflict')
}

function validateStoredEvent (value, previousHash) {
  if (!record(value)) throw errorFor('gbrain_audit_chain_corrupt')
  const keys = Object.keys(value).sort()
  const expectedKeys = [...EVENT_FIELDS].sort()
  if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index])) throw errorFor('gbrain_audit_chain_corrupt')
  if (value.schema !== GBRAIN_BRIDGE_AUDIT_SCHEMA || value.previous_hash !== previousHash) throw errorFor('gbrain_audit_chain_corrupt')
  if (typeof value.local_event_hash !== 'string' || !SHA256.test(value.local_event_hash) || eventHash(value) !== value.local_event_hash) throw errorFor('gbrain_audit_chain_corrupt')
  const { schema: _schema, previous_hash: _previousHash, local_event_hash: _localEventHash, ...input } = value
  normalizeInputEvent(input, { eventIdFactory: () => value.event_id, now: () => value.timestamp })
  if (value.previous_hash !== null && !SHA256.test(value.previous_hash)) throw errorFor('gbrain_audit_chain_corrupt')
  return value
}

async function secureDirectory (fsImpl, stateDir, { create = true } = {}) {
  if (typeof stateDir !== 'string' || stateDir.length === 0 || stateDir.includes('\0')) throw errorFor('gbrain_audit_state_dir_invalid')
  const absolute = resolve(stateDir)
  try {
    const before = await fsImpl.lstat(absolute)
    if (!before.isDirectory() || (before.mode & 0o077) !== 0) throw errorFor('gbrain_audit_directory_untrusted')
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
    if (!create) throw errorFor('gbrain_audit_directory_missing')
    await fsImpl.mkdir(absolute, { recursive: true, mode: 0o700 })
    const created = await fsImpl.lstat(absolute)
    if (!created.isDirectory() || (created.mode & 0o077) !== 0) throw errorFor('gbrain_audit_directory_untrusted')
  }
  return absolute
}

async function secureFile (fsImpl, path, { allowMissing = true } = {}) {
  try {
    const info = await fsImpl.lstat(path)
    if (!info.isFile() || (info.mode & 0o077) !== 0) throw errorFor('gbrain_audit_file_untrusted')
    return info
  } catch (error) {
    if (allowMissing && error?.code === 'ENOENT') return null
    throw error
  }
}

async function readRotationManifest (fsImpl, stateDir) {
  const path = join(stateDir, '.chain.json')
  const info = await secureFile(fsImpl, path)
  if (!info) return null
  let parsed
  try { parsed = JSON.parse(await fsImpl.readFile(path, 'utf8')) } catch { throw errorFor('gbrain_audit_chain_corrupt') }
  if (!record(parsed) || Object.keys(parsed).length !== 2 || parsed.schema !== GBRAIN_BRIDGE_AUDIT_SCHEMA || (parsed.base_hash !== null && (!SHA256.test(parsed.base_hash) || typeof parsed.base_hash !== 'string'))) throw errorFor('gbrain_audit_chain_corrupt')
  return parsed.base_hash
}

async function writeRotationManifest (fsImpl, stateDir, baseHash) {
  if (baseHash !== null && !SHA256.test(baseHash)) throw errorFor('gbrain_audit_chain_corrupt')
  const path = join(stateDir, '.chain.json')
  await secureFile(fsImpl, path)
  const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | (constants.O_NOFOLLOW || 0)
  const handle = await fsImpl.open(path, flags, 0o600)
  try {
    const info = await handle.stat()
    if (!info.isFile() || (info.mode & 0o077) !== 0) throw errorFor('gbrain_audit_file_untrusted')
    await handle.write(`${JSON.stringify({ schema: GBRAIN_BRIDGE_AUDIT_SCHEMA, base_hash: baseHash })}\n`, null, 'utf8')
    await handle.sync()
  } finally {
    await handle.close()
  }
}

async function segmentPaths (fsImpl, stateDir, maxSegments) {
  const names = await fsImpl.readdir(stateDir)
  const segments = []
  for (const name of names) {
    const match = SEGMENT_PATTERN.exec(name)
    if (!match) continue
    const path = join(stateDir, name)
    await secureFile(fsImpl, path, { allowMissing: false })
    segments.push({ number: Number(match[1]), path })
  }
  segments.sort((left, right) => left.number - right.number)
  if (segments.length > maxSegments) throw errorFor('gbrain_audit_segments_exceeded')
  for (let index = 1; index < segments.length; index += 1) {
    if (segments[index].number !== segments[index - 1].number + 1) throw errorFor('gbrain_audit_segment_sequence_invalid')
  }
  return segments
}

async function readLedger ({ stateDir, maxFileBytes = DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_FILE_BYTES, maxSegments = DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_SEGMENTS, fsImpl = fs } = {}) {
  const absolute = await secureDirectory(fsImpl, stateDir || DEFAULT_GBRAIN_BRIDGE_AUDIT_STATE_DIR, { create: false })
  if (!Number.isSafeInteger(maxFileBytes) || maxFileBytes < 512 || maxFileBytes > 4 * 1024 * 1024) throw errorFor('gbrain_audit_max_file_bytes_invalid')
  if (!Number.isSafeInteger(maxSegments) || maxSegments < 1 || maxSegments > 128) throw errorFor('gbrain_audit_max_segments_invalid')
  const segments = await segmentPaths(fsImpl, absolute, maxSegments)
  const baseHash = await readRotationManifest(fsImpl, absolute)
  if (baseHash !== null && segments.length === 0) throw errorFor('gbrain_audit_chain_corrupt')
  const events = []
  const eventIds = new Set()
  const idempotencyKeys = new Set()
  const issues = []
  let previousHash = baseHash
  const segmentHashes = []
  for (const segment of segments) {
    const segmentInfo = await secureFile(fsImpl, segment.path, { allowMissing: false })
    if (segmentInfo.size > maxFileBytes) throw errorFor('gbrain_audit_file_too_large')
    const text = await fsImpl.readFile(segment.path, 'utf8')
    if (text.length === 0) continue
    const lines = text.split('\n')
    const trailingPartial = lines.at(-1) !== ''
    lines.pop()
    if (trailingPartial) issues.push('partial_line')
    let segmentLastHash = previousHash
    for (const line of lines) {
      if (line === '') throw errorFor('gbrain_audit_chain_corrupt')
      let parsed
      try { parsed = JSON.parse(line) } catch { throw errorFor('gbrain_audit_chain_corrupt') }
      const event = validateStoredEvent(parsed, previousHash)
      if (eventIds.has(event.event_id)) throw errorFor('gbrain_audit_event_duplicate')
      if (event.idempotency_key && idempotencyKeys.has(event.idempotency_key)) throw errorFor('gbrain_audit_idempotency_duplicate')
      assertRememberTerminalInvariant(events, event)
      eventIds.add(event.event_id)
      if (event.idempotency_key) idempotencyKeys.add(event.idempotency_key)
      events.push(Object.freeze(event))
      previousHash = event.local_event_hash
      segmentLastHash = previousHash
    }
    segmentHashes.push(Object.freeze({ number: segment.number, last_hash: segmentLastHash }))
  }
  const current = segments.at(-1)
  const fileMode = current ? (await secureFile(fsImpl, current.path, { allowMissing: false })).mode & 0o777 : 0o600
  const directoryInfo = await fsImpl.lstat(absolute)
  return Object.freeze({
    complete: issues.length === 0,
    issues: Object.freeze([...new Set(issues)]),
    events: Object.freeze(events),
    segments: segments.length,
    file_mode: fileMode,
    directory_mode: directoryInfo.mode & 0o777,
    file_path: current?.path || join(absolute, 'events-000001.jsonl'),
    last_hash: previousHash,
    segment_hashes: Object.freeze(segmentHashes)
  })
}

async function syncDirectory (fsImpl, directory) {
  const handle = await fsImpl.open(directory, DIRECTORY_OPEN_FLAGS)
  try {
    if (!handle || typeof handle.sync !== 'function') throw errorFor('gbrain_audit_directory_sync_unavailable')
    await handle.sync()
  } finally {
    try { await handle?.close?.() } catch { /* Preserve the sync result. */ }
  }
}

function defaultProcessAlive (pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code === 'EPERM'
  }
}

async function lockOwner (fsImpl, lockPath) {
  try {
    const value = JSON.parse(await fsImpl.readFile(lockPath, 'utf8'))
    if (!record(value) || typeof value.owner !== 'string' || !SAFE_TOKEN.test(value.owner) || !Number.isSafeInteger(value.pid) || value.pid < 1) return null
    return value
  } catch {
    return null
  }
}

async function recoverStaleLock ({ fsImpl, stateDir, lockPath, observed, lockTtlMs, lockNow, isProcessAlive }) {
  if (!Number.isFinite(observed?.mtimeMs) || lockNow() - observed.mtimeMs <= lockTtlMs) return false
  const owner = await lockOwner(fsImpl, lockPath)
  if (!owner || owner.pid === process.pid || isProcessAlive(owner.pid)) return false
  const confirmed = await fsImpl.lstat(lockPath).catch((error) => {
    if (error?.code === 'ENOENT') return null
    throw error
  })
  if (!confirmed || !sameLockObject(observed, confirmed)) return false
  await fsImpl.unlink(lockPath)
  await syncDirectory(fsImpl, stateDir)
  return true
}

async function acquireLock (fsImpl, stateDir, { lockTtlMs, lockWaitMs, lockNow, isProcessAlive, lockTokenFactory }) {
  const lockPath = join(stateDir, '.lock')
  const started = lockNow()
  while (lockNow() - started <= lockWaitMs) {
    try {
      const handle = await fsImpl.open(lockPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600)
      try {
        const info = await handle.stat()
        if (!info.isFile() || (info.mode & 0o077) !== 0 || info.nlink !== 1) throw errorFor('gbrain_audit_lock_untrusted')
        const owner = lockTokenFactory()
        if (typeof owner !== 'string' || !SAFE_TOKEN.test(owner)) throw errorFor('gbrain_audit_lock_owner_invalid')
        await handle.write(JSON.stringify({ schema: GBRAIN_BRIDGE_AUDIT_SCHEMA, owner, pid: process.pid }), null, 'utf8')
        await handle.sync()
        const lockInfo = await handle.stat()
        if (!lockInfo.isFile() || (lockInfo.mode & 0o077) !== 0 || lockInfo.nlink !== 1) throw errorFor('gbrain_audit_lock_untrusted')
        return {
          handle,
          lockPath,
          info: lockInfo,
          async release () {
            try {
              const current = await fsImpl.lstat(lockPath)
              if (!sameLockObject(lockInfo, current)) return
              const confirmed = await fsImpl.lstat(lockPath)
              if (!sameLockObject(lockInfo, confirmed)) return
              await fsImpl.unlink(lockPath)
              await syncDirectory(fsImpl, stateDir)
            } finally {
              await handle.close().catch(() => {})
            }
          }
        }
      } catch (error) {
        await handle.close().catch(() => {})
        throw error
      }
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error
      let info
      try { info = await fsImpl.lstat(lockPath) } catch (lstatError) {
        if (lstatError?.code === 'ENOENT') continue
        throw lstatError
      }
      if (info.nlink === 0) continue
      if (!info.isFile() || (info.mode & 0o077) !== 0 || info.nlink !== 1) throw errorFor('gbrain_audit_lock_untrusted')
      if (await recoverStaleLock({ fsImpl, stateDir, lockPath, observed: info, lockTtlMs, lockNow, isProcessAlive })) continue
      await new Promise((resolveWait) => setTimeout(resolveWait, 5))
    }
  }
  throw errorFor('gbrain_audit_lock_timeout')
}

export { acquireLock as acquireGbrainPrivateStateLock }

function enqueue (stateDir, task) {
  const previous = QUEUES.get(stateDir) || Promise.resolve()
  const current = previous.catch(() => {}).then(task)
  const settled = current.then(() => undefined, () => undefined)
  QUEUES.set(stateDir, settled)
  void settled.then(() => {
    if (QUEUES.get(stateDir) === settled) QUEUES.delete(stateDir)
  })
  return current
}

export function createGbrainBridgeAuditLedger ({
  stateDir = DEFAULT_GBRAIN_BRIDGE_AUDIT_STATE_DIR,
  maxFileBytes = DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_FILE_BYTES,
  maxSegments = DEFAULT_GBRAIN_BRIDGE_AUDIT_MAX_SEGMENTS,
  now = () => new Date().toISOString(),
  eventIdFactory = () => `oos-audit-event:${createHash('sha256').update(`${Date.now()}-${Math.random()}`).digest('hex').slice(0, 24)}`,
  fsImpl = fs,
  lockTtlMs = AUDIT_LOCK_TTL_MS,
  lockWaitMs = AUDIT_LOCK_WAIT_MS,
  isProcessAlive = defaultProcessAlive,
  lockTokenFactory = () => `audit-lock:${process.pid}:${createHash('sha256').update(`${Date.now()}-${Math.random()}`).digest('hex').slice(0, 24)}`,
  lockNow = () => Date.now()
} = {}) {
  if (!Number.isSafeInteger(maxFileBytes) || maxFileBytes < 512 || maxFileBytes > 4 * 1024 * 1024) throw errorFor('gbrain_audit_max_file_bytes_invalid')
  if (!Number.isSafeInteger(maxSegments) || maxSegments < 1 || maxSegments > 128) throw errorFor('gbrain_audit_max_segments_invalid')
  if (typeof eventIdFactory !== 'function') throw errorFor('gbrain_audit_event_id_factory_invalid')
  if (!fsImpl || typeof fsImpl !== 'object') throw errorFor('gbrain_audit_filesystem_invalid')
  if (![lockTtlMs, lockWaitMs].every((value) => Number.isSafeInteger(value) && value > 0)) throw errorFor('gbrain_audit_lock_timing_invalid')
  if (typeof isProcessAlive !== 'function' || typeof lockTokenFactory !== 'function' || typeof lockNow !== 'function') throw errorFor('gbrain_audit_lock_seam_invalid')
  const absoluteStateDir = resolve(stateDir)
  const initialFilePath = join(absoluteStateDir, 'events-000001.jsonl')
  let currentFilePath = initialFilePath

  async function recordEvent (input) {
    return enqueue(absoluteStateDir, async () => {
      const directory = await secureDirectory(fsImpl, absoluteStateDir)
      const lock = await acquireLock(fsImpl, directory, { lockTtlMs, lockWaitMs, lockNow, isProcessAlive, lockTokenFactory })
      try {
        const before = await readLedger({ stateDir: directory, maxFileBytes, maxSegments, fsImpl })
        if (!before.complete) throw errorFor('gbrain_audit_partial_line')
        const normalized = normalizeInputEvent(input, { now, eventIdFactory })
        const sameEventId = before.events.find((event) => event.event_id === normalized.event_id)
        if (sameEventId) {
          if (sameIdempotentEvent(sameEventId, normalized)) return sameEventId
          throw errorFor('gbrain_audit_event_duplicate')
        }
        const sameIdempotency = normalized.idempotency_key ? before.events.find((event) => event.idempotency_key === normalized.idempotency_key) : null
        if (sameIdempotency) {
          if (sameIdempotentEvent(sameIdempotency, normalized)) return sameIdempotency
          throw errorFor('gbrain_audit_idempotency_conflict')
        }
        assertRememberTerminalInvariant(before.events, normalized)
        let segments = await segmentPaths(fsImpl, directory, maxSegments)
        if (segments.length === 0) segments = [{ number: 1, path: initialFilePath }]
        let current = segments.at(-1)
        const lineWithoutHash = { ...normalized, previous_hash: before.last_hash }
        const stored = { ...lineWithoutHash, local_event_hash: eventHash(lineWithoutHash) }
        const serialized = `${JSON.stringify(stored)}\n`
        const bytes = Buffer.byteLength(serialized, 'utf8')
        const currentInfo = await secureFile(fsImpl, current.path)
        const currentBytes = currentInfo ? currentInfo.size : 0
        if (currentBytes > 0 && currentBytes + bytes > maxFileBytes) {
          if (segments.length >= maxSegments) {
            const removeCount = segments.length - maxSegments + 1
            const baseHash = before.segment_hashes[removeCount - 1]?.last_hash
            if (!baseHash) throw errorFor('gbrain_audit_chain_corrupt')
            for (const segment of segments.slice(0, removeCount)) {
              await fsImpl.unlink(segment.path)
              await syncDirectory(fsImpl, directory)
            }
            await writeRotationManifest(fsImpl, directory, baseHash)
            segments = segments.slice(removeCount)
          }
          current = { number: current.number + 1, path: join(directory, `events-${String(current.number + 1).padStart(6, '0')}.jsonl`) }
        }
        if (bytes > maxFileBytes) throw errorFor('gbrain_audit_event_too_large')
        const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | (constants.O_NOFOLLOW || 0)
        const handle = await fsImpl.open(current.path, flags, 0o600)
        try {
          const info = await handle.stat()
          if (!info.isFile() || (info.mode & 0o077) !== 0) throw errorFor('gbrain_audit_file_untrusted')
          if (info.size + bytes > maxFileBytes) throw errorFor('gbrain_audit_capacity_exceeded')
          await handle.write(serialized, null, 'utf8')
          await handle.sync()
        } finally {
          await handle.close()
        }
        currentFilePath = current.path
        return Object.freeze(stored)
      } finally {
        await lock.release()
      }
    })
  }

  async function read () {
    return readLedger({ stateDir: absoluteStateDir, maxFileBytes, maxSegments, fsImpl })
  }

  return {
    record: recordEvent,
    read,
    get filePath () {
      return currentFilePath
    },
    stateDir: absoluteStateDir,
    maxFileBytes,
    maxSegments
  }
}

export async function readGbrainBridgeAuditLedger (options = {}) {
  return readLedger(options)
}
