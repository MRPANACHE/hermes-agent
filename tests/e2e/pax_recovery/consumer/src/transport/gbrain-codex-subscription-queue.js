import { randomBytes, createHash } from 'node:crypto'
import { constants as fsConstants } from 'node:fs'
import * as fs from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { dispatcherProcessAlive } from './pb-agent-owner-recovery.js'
import { canonicalJson, sha256 } from '../episode-ledger/hash.js'
import { createPaxHistoricalReceiptReconciler, persistPaxHistoricalEvidence } from './pax-historical-receipt.js'
import {
  GBRAIN_CODEX_SUBSCRIPTION_MODEL,
  buildGbrainCodexSubscriptionProvenance,
  gbrainCodexSubscriptionCanonicalJson,
  gbrainCodexSubscriptionFactContentHash,
  gbrainCodexSubscriptionIdempotencyKey,
  gbrainCodexSubscriptionInputHash as computeGbrainCodexSubscriptionInputHash,
  gbrainCodexSubscriptionRecoveryInputHash as computeGbrainCodexSubscriptionRecoveryInputHash,
  parseGbrainCodexSubscriptionProvenance,
  validateGbrainCodexSubscriptionPriorContext as validatePriorContextBinding
} from './gbrain-codex-subscription-provenance.js'
import {
  GBRAIN_CODEX_SUBSCRIPTION_FACT_KINDS,
  GBRAIN_CODEX_SUBSCRIPTION_KILL_SWITCH,
  GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATE_LENGTH,
  GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES
} from './gbrain-codex-subscription-extractor.js'

export { buildGbrainCodexSubscriptionProvenance, parseGbrainCodexSubscriptionProvenance } from './gbrain-codex-subscription-provenance.js'

export const GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA = 'oos.gbrain_codex_subscription_job.v1'
export const DEFAULT_GBRAIN_CODEX_SUBSCRIPTION_QUEUE_STATE_DIR = '/home/david/.local/state/operator-os/gbrain-codex-chat/subscription-extract'
export const GBRAIN_CODEX_SUBSCRIPTION_QUEUE_STATUSES = Object.freeze(['queued', 'duplicate', 'disabled', 'unavailable', 'conflict'])
export const GBRAIN_CODEX_SUBSCRIPTION_JOB_STATUSES = Object.freeze(['queued', 'running', 'retry', 'completed', 'failed'])
export const GBRAIN_CODEX_SUBSCRIPTION_MAX_ATTEMPTS = 3
export const GBRAIN_CODEX_SUBSCRIPTION_DEFAULT_LEASE_MS = 60_000
export const GBRAIN_CODEX_SUBSCRIPTION_DEFAULT_POLL_MS = 1_000
const PAX_CURATION_TRANSPORT_ERROR = 'pax_memory_curation_transport_error'
const PAX_CURATION_TRANSPORT_BACKOFF_BASE_MS = 90_000
const PAX_CURATION_TRANSPORT_BACKOFF_MAX_MS = 300_000

const QUEUE_DIRECTORY_MODE = 0o700
const QUEUE_FILE_MODE = 0o600
const MAX_JOB_BYTES = 128 * 1024
const MAX_AUDIT_REFS = 32
const HASH64 = /^[a-f0-9]{64}$/u
const HASH32 = /^[a-f0-9]{32}$/u
const SOURCE_REF = /^codex-conversations\/[a-f0-9]{32}\/[a-f0-9]{32}$/u
const JOB_ID = /^job-[a-f0-9]{64}$/u
const JOB_FILE = /^job-[a-f0-9]{64}\.json$/u
const TEMP_FILE = /^\.job-[a-f0-9]{64}\.json\.[A-Za-z0-9._:-]{1,256}\.tmp$/u
const SAFE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/u
const FACT_REF = /^gbrain-fact:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u
const LOCAL_REF = /^local-[a-z][a-z0-9-]{0,48}:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u
const O_NOFOLLOW = typeof fsConstants.O_NOFOLLOW === 'number' ? fsConstants.O_NOFOLLOW : 0
const READ_FLAGS = fsConstants.O_RDONLY | O_NOFOLLOW
const LOCK_FLAGS = fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | O_NOFOLLOW
const WRITE_FLAGS = fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | O_NOFOLLOW
const DIRECTORY_OPEN_FLAGS = fsConstants.O_RDONLY | (typeof fsConstants.O_DIRECTORY === 'number' ? fsConstants.O_DIRECTORY : 0) | O_NOFOLLOW
const JOB_KEYS = Object.freeze([
  'schema', 'job_id', 'request_id', 'origin', 'submitter', 'input_hash', 'original_turn', 'source_ref', 'session_hash', 'turn_hash', 'model',
  'candidates', 'candidate_hashes', 'status', 'attempts', 'next_attempt_at', 'created_at',
  'updated_at', 'lease', 'facts', 'remembered', 'remember_intents', 'fact_refs', 'audit_refs', 'last_error', 'completed_at'
])
const JOB_KEYS_WITHOUT_ORIGINAL_TURN = Object.freeze(JOB_KEYS.filter((key) => key !== 'original_turn'))
const LEGACY_JOB_KEYS = Object.freeze(JOB_KEYS_WITHOUT_ORIGINAL_TURN.filter((key) => !['request_id', 'origin', 'submitter'].includes(key)))
const LEGACY_JOB_KEYS_WITH_ORIGINAL_TURN = Object.freeze([...LEGACY_JOB_KEYS, 'original_turn'])
const JOB_KEYS_WITH_CONTEXT = Object.freeze([...JOB_KEYS, 'prior_context'])
const JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_CONTEXT = Object.freeze([...JOB_KEYS_WITHOUT_ORIGINAL_TURN, 'prior_context'])
const LEGACY_JOB_KEYS_WITH_CONTEXT = Object.freeze([...LEGACY_JOB_KEYS, 'prior_context'])
const LEGACY_JOB_KEYS_WITH_ORIGINAL_TURN_WITH_CONTEXT = Object.freeze([...LEGACY_JOB_KEYS_WITH_ORIGINAL_TURN, 'prior_context'])
const JOB_KEYS_WITH_RUNTIME = Object.freeze([...JOB_KEYS, 'curation_runtime'])
const JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_RUNTIME = Object.freeze([...JOB_KEYS_WITHOUT_ORIGINAL_TURN, 'curation_runtime'])
const JOB_KEYS_WITH_CONTEXT_AND_RUNTIME = Object.freeze([...JOB_KEYS_WITH_CONTEXT, 'curation_runtime'])
const JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_CONTEXT_AND_RUNTIME = Object.freeze([...JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_CONTEXT, 'curation_runtime'])
const JOB_KEYS_WITH_RECOVERY_OF = Object.freeze([...JOB_KEYS, 'recovery_of'])
const JOB_KEYS_WITH_CONTEXT_AND_RECOVERY_OF = Object.freeze([...JOB_KEYS_WITH_CONTEXT, 'recovery_of'])
const JOB_KEYS_WITH_RUNTIME_AND_RECOVERY_OF = Object.freeze([...JOB_KEYS_WITH_RUNTIME, 'recovery_of'])
const JOB_KEYS_WITH_CONTEXT_RUNTIME_AND_RECOVERY_OF = Object.freeze([...JOB_KEYS_WITH_CONTEXT_AND_RUNTIME, 'recovery_of'])
const JOB_KEYS_WITH_HISTORY_RECONCILE_AND_RUNTIME_AND_RECOVERY_OF = Object.freeze([...JOB_KEYS_WITH_RUNTIME_AND_RECOVERY_OF, 'history_reconcile'])
const JOB_KEYS_WITH_CONTEXT_HISTORY_RECONCILE_RUNTIME_AND_RECOVERY_OF = Object.freeze([...JOB_KEYS_WITH_CONTEXT_RUNTIME_AND_RECOVERY_OF, 'history_reconcile'])
const FACT_KEYS = Object.freeze(['candidate_index', 'fact', 'entity', 'kind', 'confidence', 'source_ref', 'session_hash', 'turn_hash', 'content_hash', 'idempotency_key'])
const REDACTED_FACT_KEYS = Object.freeze(['candidate_index', 'kind', 'confidence', 'content_hash', 'idempotency_key'])
const REMEMBERED_KEYS = Object.freeze(['candidate_index', 'idempotency_key', 'fact_refs', 'status'])
const REMEMBER_INTENT_KEYS = Object.freeze(['candidate_index', 'idempotency_key', 'fact', 'provenance', 'entity', 'kind', 'confidence', 'visibility', 'content_hash', 'status', 'fact_refs'])
const REMEMBER_INTENT_STATUSES = Object.freeze(['pending', 'ambiguous', 'completed'])
const ORIGINAL_TURN_MAX_LENGTH = 12_000

function errorFor (code) {
  return Object.assign(new Error(code), { code })
}

function isRecord (value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function exactKeys (value, expected) {
  if (!isRecord(value)) return false
  const actual = Object.keys(value).sort()
  const sorted = [...expected].sort()
  return actual.length === sorted.length && actual.every((key, index) => key === sorted[index])
}

function exactKeysWithOptionalPriorContext (value, expected) {
  return exactKeys(value, expected) || exactKeys(value, [...expected, 'prior_context'])
}

function exactKeysWithOptionalMetadata (value, expected, { allowRecovery = false } = {}) {
  const optional = ['prior_context', 'origin', 'submitter', ...(allowRecovery ? ['recovery_of'] : [])]
  const variants = [[]]
  for (const key of optional) {
    for (const current of [...variants]) variants.push([...current, key])
  }
  return variants.some((suffix) => exactKeys(value, [...expected, ...suffix]))
}

function knownKeys (value, expected) {
  return isRecord(value) && Object.keys(value).every((key) => expected.includes(key))
}

function boundedText (value, max) {
  return typeof value === 'string' && value.length > 0 && value.length <= max && !value.includes('\0')
}

function validatePriorContext (value, sessionHash, sourceRef) {
  try { return validatePriorContextBinding(value, sessionHash, sourceRef) } catch { throw errorFor('gbrain_codex_subscription_input_invalid') }
}

function inputBinding (input) {
  return {
    ...(Object.hasOwn(input, 'original_turn') ? { original_turn: input.original_turn } : {}),
    ...(Object.hasOwn(input, 'prior_context') ? { prior_context: input.prior_context } : {}),
    source_ref: input.source_ref,
    session_hash: input.session_hash,
    turn_hash: input.turn_hash,
    model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
    candidates: input.candidates
  }
}

export function gbrainCodexSubscriptionInputHash (input) {
  return computeGbrainCodexSubscriptionInputHash(validateInput(input))
}

function nowMilliseconds (value) {
  const raw = typeof value === 'function' ? value() : value
  const numeric = typeof raw === 'number' ? raw : Date.parse(String(raw))
  if (!Number.isFinite(numeric)) throw errorFor('gbrain_codex_subscription_clock_invalid')
  return numeric
}

function iso (value) {
  const timestamp = new Date(nowMilliseconds(value)).toISOString()
  if (!Number.isFinite(Date.parse(timestamp))) throw errorFor('gbrain_codex_subscription_timestamp_invalid')
  return timestamp
}

function uniqueToken (prefix) {
  return `${prefix}:${process.pid}:${randomBytes(12).toString('hex')}`
}

function validateInput (value, { allowRecovery = false } = {}) {
  const hasOriginalTurn = Object.hasOwn(value, 'original_turn')
  const expected = hasOriginalTurn ? ['candidates', 'original_turn', 'source_ref', 'session_hash', 'turn_hash'] : ['candidates', 'source_ref', 'session_hash', 'turn_hash']
  if (!exactKeysWithOptionalMetadata(value, expected, { allowRecovery })) throw errorFor('gbrain_codex_subscription_input_invalid')
  if (!Array.isArray(value.candidates) || value.candidates.length > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || (hasOriginalTurn ? !boundedText(value.original_turn, ORIGINAL_TURN_MAX_LENGTH) : value.candidates.length === 0)) throw errorFor('gbrain_codex_subscription_input_invalid')
  if (!SOURCE_REF.test(value.source_ref) || !HASH32.test(value.session_hash) || !HASH32.test(value.turn_hash) || (value.origin !== undefined && !['human', 'agent'].includes(value.origin)) || (value.submitter !== undefined && (!boundedText(value.submitter, 256) || !SAFE_TOKEN.test(value.submitter)))) throw errorFor('gbrain_codex_subscription_input_invalid')
  const candidates = value.candidates.map((candidate) => {
    if (!boundedText(candidate, GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATE_LENGTH)) throw errorFor('gbrain_codex_subscription_input_invalid')
    return candidate
  })
  const priorContext = validatePriorContext(value.prior_context, value.session_hash, value.source_ref)
  const recoveryOf = Object.hasOwn(value, 'recovery_of') ? value.recovery_of : undefined
  if (recoveryOf !== undefined && (!allowRecovery || !validateRecoveryOf(recoveryOf) || recoveryOf.source_ref !== value.source_ref)) throw errorFor('gbrain_codex_subscription_input_invalid')
  return Object.freeze({
    ...(hasOriginalTurn ? { original_turn: value.original_turn } : {}),
    ...(priorContext.length > 0 ? { prior_context: priorContext } : {}),
    candidates: Object.freeze(candidates),
    source_ref: value.source_ref,
    session_hash: value.session_hash,
    turn_hash: value.turn_hash,
    ...(value.origin !== undefined ? { origin: value.origin } : {}),
    ...(value.submitter !== undefined ? { submitter: value.submitter } : {}),
    ...(recoveryOf !== undefined ? { recovery_of: Object.freeze({ ...recoveryOf }) } : {})
  })
}

function inputHashFor (input) {
  return Object.hasOwn(input, 'recovery_of')
    ? computeGbrainCodexSubscriptionRecoveryInputHash(input)
    : computeGbrainCodexSubscriptionInputHash(input)
}

export function validateGbrainCodexSubscriptionInput (value) {
  return validateInput(value)
}

function sameFile (left, right) {
  return left?.dev !== undefined && left?.ino !== undefined && left.dev === right?.dev && left.ino === right?.ino
}

function sameLockObject (left, right) {
  return sameFile(left, right) && left.size === right?.size && left.mtimeMs === right?.mtimeMs
}

function expectedUid () {
  return typeof process.getuid === 'function' ? process.getuid() : undefined
}

function validateDirectoryInfo (info) {
  if (!info?.isDirectory?.() || (expectedUid() !== undefined && info.uid !== expectedUid()) || (info.mode & 0o7777) !== QUEUE_DIRECTORY_MODE) throw errorFor('gbrain_codex_subscription_directory_untrusted')
}

function validateFileInfo (info, maxBytes = MAX_JOB_BYTES) {
  if (!info?.isFile?.() || (expectedUid() !== undefined && info.uid !== expectedUid()) || (info.mode & 0o7777) !== QUEUE_FILE_MODE || info.nlink !== 1) throw errorFor('gbrain_codex_subscription_file_untrusted')
  if (!Number.isSafeInteger(info.size) || info.size > maxBytes) throw errorFor('gbrain_codex_subscription_file_oversized')
}

async function ensureDirectory (fsImpl, path, { recursive = false } = {}) {
  const absolute = resolve(path)
  let current = '/'
  for (const component of absolute.split('/').filter(Boolean)) {
    current = join(current, component)
    let info
    try {
      info = await fsImpl.lstat(current)
    } catch (error) {
      if (error?.code !== 'ENOENT' || (!recursive && current !== absolute)) throw error
      try { await fsImpl.mkdir(current, { mode: QUEUE_DIRECTORY_MODE }) } catch (mkdirError) {
        if (mkdirError?.code !== 'EEXIST') throw mkdirError
      }
      info = await fsImpl.lstat(current)
    }
    if (!info.isDirectory()) throw errorFor('gbrain_codex_subscription_directory_untrusted')
    if (current === absolute) validateDirectoryInfo(info)
  }
  return path
}

async function syncDirectory (fsImpl, directory) {
  const handle = await fsImpl.open(directory, DIRECTORY_OPEN_FLAGS)
  try {
    if (!handle || typeof handle.sync !== 'function') throw errorFor('gbrain_codex_subscription_directory_sync_unavailable')
    await handle.sync()
  } finally {
    try { await handle?.close?.() } catch { /* Preserve the sync result. */ }
  }
}

async function readBoundedHandle (handle, maxBytes) {
  const buffer = Buffer.alloc(maxBytes + 1)
  let offset = 0
  while (offset < buffer.length) {
    const result = await handle.read(buffer, offset, buffer.length - offset, offset)
    if (!Number.isSafeInteger(result?.bytesRead) || result.bytesRead < 0 || result.bytesRead > buffer.length - offset) throw errorFor('gbrain_codex_subscription_read_invalid')
    if (result.bytesRead === 0) break
    offset += result.bytesRead
  }
  return buffer.subarray(0, offset)
}

async function readQueueBytes (fsImpl, path, maxBytes = MAX_JOB_BYTES) {
  const before = await fsImpl.lstat(path)
  validateFileInfo(before, maxBytes)
  let handle = null
  try {
    handle = await fsImpl.open(path, READ_FLAGS)
    const opened = await handle.stat()
    validateFileInfo(opened, maxBytes)
    if (!sameFile(before, opened)) throw errorFor('gbrain_codex_subscription_file_changed')
    const bytes = await readBoundedHandle(handle, maxBytes)
    const after = await handle.stat()
    validateFileInfo(after, maxBytes)
    if (!sameFile(opened, after) || bytes.length > maxBytes) throw errorFor('gbrain_codex_subscription_file_changed')
    return bytes
  } finally {
    await handle?.close().catch(() => {})
  }
}

async function readJsonFile (fsImpl, path) {
  const bytes = await readQueueBytes(fsImpl, path)
  try { return JSON.parse(bytes.toString('utf8')) } catch { throw errorFor('gbrain_codex_subscription_json_invalid') }
}

async function atomicWriteJson (path, value, { replace = true, tokenFactory = () => uniqueToken('tmp'), fsImpl = fs } = {}) {
  const directory = dirname(path)
  const serialized = `${JSON.stringify(value)}\n`
  if (Buffer.byteLength(serialized, 'utf8') > MAX_JOB_BYTES) throw errorFor('gbrain_codex_subscription_job_oversized')
  let destination
  try {
    destination = await fsImpl.lstat(path)
    validateFileInfo(destination)
    if (!replace) throw errorFor('gbrain_codex_subscription_duplicate')
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }

  const tempToken = String(tokenFactory()).replace(/[^A-Za-z0-9._:-]/gu, '-')
  if (!boundedText(tempToken, 256) || !SAFE_TOKEN.test(tempToken)) throw errorFor('gbrain_codex_subscription_temp_filename_invalid')
  const tempName = `.${path.split('/').at(-1)}.${tempToken}.tmp`
  const tempPath = join(directory, tempName)
  let handle = null
  let created = false
  try {
    handle = await fsImpl.open(tempPath, WRITE_FLAGS, QUEUE_FILE_MODE)
    created = true
    const info = await handle.stat()
    validateFileInfo(info)
    await handle.write(serialized, null, 'utf8')
    await handle.sync()
    await handle.chmod(QUEUE_FILE_MODE)
    await handle.close()
    handle = null
    await fsImpl.rename(tempPath, path)
    await syncDirectory(fsImpl, directory)
    const finalInfo = await fsImpl.lstat(path)
    validateFileInfo(finalInfo)
    const finalValue = await readJsonFile(fsImpl, path)
    if (gbrainCodexSubscriptionCanonicalJson(finalValue) !== gbrainCodexSubscriptionCanonicalJson(value)) throw errorFor('gbrain_codex_subscription_write_verification_failed')
  } catch (error) {
    await handle?.close().catch(() => {})
    if (created) {
      try {
        await fsImpl.unlink(tempPath)
        await syncDirectory(fsImpl, directory)
      } catch { /* Preserve the original operation failure. */ }
    }
    throw error
  }
}

function validateAuditRef (value) {
  return typeof value === 'string' && (FACT_REF.test(value) || LOCAL_REF.test(value))
}

function enqueueAuditRef (inputHash) {
  return `local-csx-queue:${inputHash}`
}

function auditIdentity (input) {
  const correlation = createHash('sha256').update(`${input.session_hash}\0${input.turn_hash}`, 'utf8').digest('hex').slice(0, 32)
  return {
    session_ref: `codex-session:${input.session_hash}`,
    turn_ref: `codex-turn:${input.turn_hash}`,
    correlation_ref: `codex-correlation:${correlation}`,
    source_ref: input.source_ref
  }
}

function validateAuditKey (inputHash, operation, phase, idempotencyKey) {
  if (typeof idempotencyKey !== 'string' || !SAFE_TOKEN.test(idempotencyKey)) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
  if (operation === 'remember') {
    const suffix = phase === 'attempted' ? ':attempted' : phase === 'completed' ? '' : ':failed'
    if (!HASH64.test(idempotencyKey.replace(suffix, '')) || idempotencyKey !== `${idempotencyKey.replace(suffix, '')}${suffix}`) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
    return
  }
  if (operation === 'codex_subscription_enqueue') {
    const expected = phase === 'attempted' ? `${inputHash}:enqueue:attempted` : phase === 'failed' ? `${inputHash}:enqueue:failed` : null
    if (expected && idempotencyKey !== expected) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
    if (phase === 'completed' && !new RegExp(`^${inputHash}:enqueue:(?:queued|duplicate):completed$`, 'u').test(idempotencyKey)) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
    return
  }
  if (operation === 'codex_subscription_extract') {
    const expected = new RegExp(`^${inputHash}:extract:([1-8]):${phase}$`, 'u')
    if (!expected.test(idempotencyKey)) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
    return
  }
  if (operation === 'codex_subscription_curate') {
    const expected = new RegExp(`^${inputHash}:curate:([1-8]):${phase}$`, 'u')
    if (!expected.test(idempotencyKey)) throw errorFor('gbrain_codex_subscription_audit_idempotency_invalid')
    return
  }
  throw errorFor('gbrain_codex_subscription_audit_operation_invalid')
}

function validateAuditArguments ({ input, operation, transportVerb, phase, idempotencyKey, resultRef, resultFactRefs, resultCount, errorCode }) {
  const validInput = validateInput(input, { allowRecovery: true })
  const inputHash = inputHashFor(validInput)
  if (!['codex_subscription_enqueue', 'codex_subscription_extract', 'codex_subscription_curate', 'remember'].includes(operation)) throw errorFor('gbrain_codex_subscription_audit_operation_invalid')
  if (!['attempted', 'completed', 'failed'].includes(phase)) throw errorFor('gbrain_codex_subscription_audit_phase_invalid')
  if (transportVerb !== operation) throw errorFor('gbrain_codex_subscription_audit_transport_invalid')
  validateAuditKey(inputHash, operation, phase, idempotencyKey)
  if (resultRef !== null && !validateAuditRef(resultRef)) throw errorFor('gbrain_codex_subscription_audit_result_ref_invalid')
  if (!Array.isArray(resultFactRefs) || resultFactRefs.length > MAX_AUDIT_REFS || new Set(resultFactRefs).size !== resultFactRefs.length || !resultFactRefs.every((ref) => validateAuditRef(ref))) throw errorFor('gbrain_codex_subscription_audit_result_fact_refs_invalid')
  if (!Number.isSafeInteger(resultCount) || resultCount < 0) throw errorFor('gbrain_codex_subscription_audit_result_count_invalid')
  if (errorCode !== null && (typeof errorCode !== 'string' || !SAFE_TOKEN.test(errorCode))) throw errorFor('gbrain_codex_subscription_audit_error_code_invalid')

  if (phase === 'attempted') {
    if (resultRef !== null || resultFactRefs.length !== 0 || resultCount !== 0 || errorCode !== null) throw errorFor('gbrain_codex_subscription_audit_attempted_result_invalid')
  } else if (phase === 'failed') {
    if (resultRef !== null || resultFactRefs.length !== 0 || resultCount !== 0 || errorCode === null) throw errorFor('gbrain_codex_subscription_audit_failed_result_invalid')
  } else {
    if (errorCode !== null) throw errorFor('gbrain_codex_subscription_audit_completed_error_invalid')
    if (operation === 'codex_subscription_enqueue') {
      const duplicate = idempotencyKey.endsWith(':enqueue:duplicate:completed')
      if (resultRef !== `local-csx-queue:${inputHash}` || resultFactRefs.length !== 0 || resultCount !== (duplicate ? 0 : 1)) throw errorFor('gbrain_codex_subscription_audit_enqueue_result_invalid')
    } else if (operation === 'codex_subscription_extract') {
      if (resultRef !== `local-csx-extract:${inputHash}` || resultFactRefs.length !== 0 || resultCount > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES) throw errorFor('gbrain_codex_subscription_audit_extract_result_invalid')
    } else if (operation === 'codex_subscription_curate') {
      if (resultRef !== `local-pax-curation:${inputHash}` || resultFactRefs.length > MAX_AUDIT_REFS || resultCount > 4) throw errorFor('gbrain_codex_subscription_audit_curate_result_invalid')
    } else {
      if (!FACT_REF.test(resultRef || '') || resultFactRefs.length !== 1 || resultFactRefs[0] !== resultRef || resultCount !== 1) throw errorFor('gbrain_codex_subscription_audit_remember_result_invalid')
    }
  }
  return { input: validInput, inputHash }
}

export function recordGbrainCodexSubscriptionAudit (args) {
  if (typeof args?.auditLedger?.record !== 'function') return Promise.reject(errorFor('gbrain_codex_subscription_audit_required'))
  try {
    const normalized = {
      ...args,
      transportVerb: Object.hasOwn(args, 'transportVerb') ? args.transportVerb : args.operation,
      resultRef: Object.hasOwn(args, 'resultRef') ? args.resultRef : null,
      resultFactRefs: Object.hasOwn(args, 'resultFactRefs') ? args.resultFactRefs : [],
      resultCount: Object.hasOwn(args, 'resultCount') ? args.resultCount : 0,
      errorCode: Object.hasOwn(args, 'errorCode') ? args.errorCode : null
    }
    const { input } = validateAuditArguments(normalized)
    const event = {
      operation: normalized.operation,
      transport_verb: normalized.transportVerb,
      phase: normalized.phase,
      ...auditIdentity(input),
      idempotency_key: normalized.idempotencyKey,
      result_ref: normalized.resultRef,
      result_fact_refs: [...normalized.resultFactRefs],
      result_count: normalized.resultCount,
      error_code: normalized.errorCode
    }
    return awaitable(normalized.auditLedger.record(event))
  } catch (error) { return Promise.reject(error) }
}

function awaitable (value) {
  return Promise.resolve(value)
}

function queueResult (status, inputHash, extra = {}) {
  return Object.freeze({ status, queue_status: status, input_hash: inputHash, ...extra })
}

function validateLease (lease) {
  if (lease === null) return true
  return exactKeys(lease, ['owner_id', 'token', 'expires_at']) && boundedText(lease.owner_id, 256) && SAFE_TOKEN.test(lease.owner_id) && boundedText(lease.token, 256) && SAFE_TOKEN.test(lease.token) && typeof lease.expires_at === 'string' && Number.isFinite(Date.parse(lease.expires_at))
}

function validateFact (fact) {
  if (!isRecord(fact) || !knownKeys(fact, FACT_KEYS) || !Number.isSafeInteger(fact.candidate_index) || fact.candidate_index < 0 || fact.candidate_index >= GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || !GBRAIN_CODEX_SUBSCRIPTION_FACT_KINDS.includes(fact.kind) || typeof fact.confidence !== 'number' || !Number.isFinite(fact.confidence) || fact.confidence < 0 || fact.confidence > 1 || !HASH64.test(fact.content_hash) || !HASH64.test(fact.idempotency_key)) return false
  if (fact.fact !== undefined && !boundedText(fact.fact, GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATE_LENGTH)) return false
  if (fact.fact !== undefined && gbrainCodexSubscriptionFactContentHash(fact.fact) !== fact.content_hash) return false
  if (fact.entity !== undefined && !boundedText(fact.entity, 128)) return false
  if (fact.source_ref !== undefined && !SOURCE_REF.test(fact.source_ref)) return false
  if (fact.session_hash !== undefined && !HASH32.test(fact.session_hash)) return false
  if (fact.turn_hash !== undefined && !HASH32.test(fact.turn_hash)) return false
  return true
}

function factIdempotencyBinding (job, fact) {
  return {
    ...(Object.hasOwn(job, 'original_turn') ? { original_turn: job.original_turn } : {}),
    ...(Object.hasOwn(job, 'prior_context') ? { prior_context: job.prior_context } : {}),
    candidates: job.candidates,
    candidate_index: fact.candidate_index,
    confidence: fact.confidence,
    content_hash: fact.content_hash,
    entity: fact.entity,
    fact: fact.fact,
    input_hash: job.input_hash,
    kind: fact.kind,
    model: job.model,
    session_hash: job.session_hash,
    source_ref: job.source_ref,
    turn_hash: job.turn_hash
  }
}

function validateRedactedFact (fact) {
  return exactKeys(fact, REDACTED_FACT_KEYS) && Number.isSafeInteger(fact.candidate_index) && fact.candidate_index >= 0 && GBRAIN_CODEX_SUBSCRIPTION_FACT_KINDS.includes(fact.kind) && typeof fact.confidence === 'number' && Number.isFinite(fact.confidence) && fact.confidence >= 0 && fact.confidence <= 1 && HASH64.test(fact.content_hash) && HASH64.test(fact.idempotency_key)
}

function validateRemembered (value) {
  return exactKeys(value, REMEMBERED_KEYS) && Number.isSafeInteger(value.candidate_index) && value.candidate_index >= 0 && HASH64.test(value.idempotency_key) && Array.isArray(value.fact_refs) && value.fact_refs.length > 0 && value.fact_refs.every((ref) => FACT_REF.test(ref)) && ['inserted', 'duplicate', 'superseded'].includes(value.status)
}

function validateRememberIntent (value) {
  const provenance = exactKeys(value, REMEMBER_INTENT_KEYS) ? parseGbrainCodexSubscriptionProvenance(value.provenance) : null
  const contentHash = boundedText(value?.fact, GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATE_LENGTH) ? gbrainCodexSubscriptionFactContentHash(value.fact) : null
  return provenance !== null && Number.isSafeInteger(value.candidate_index) && value.candidate_index >= 0 && HASH64.test(value.idempotency_key) && boundedText(value.entity, 128) && GBRAIN_CODEX_SUBSCRIPTION_FACT_KINDS.includes(value.kind) && typeof value.confidence === 'number' && Number.isFinite(value.confidence) && value.confidence >= 0 && value.confidence <= 1 && value.visibility === 'world' && HASH64.test(value.content_hash) && value.content_hash === contentHash && provenance.idempotency_key === value.idempotency_key && REMEMBER_INTENT_STATUSES.includes(value.status) && Array.isArray(value.fact_refs) && value.fact_refs.length <= MAX_AUDIT_REFS && value.fact_refs.every((ref) => FACT_REF.test(ref)) && (value.status === 'completed' ? value.fact_refs.length > 0 : value.fact_refs.length === 0)
}

function validateRecoveryOf (value) {
  return exactKeys(value, ['job_id', 'input_hash', 'request_id', 'source_ref', 'last_error']) && JOB_ID.test(value.job_id) && HASH64.test(value.input_hash) && value.job_id === `job-${value.input_hash}` && boundedText(value.request_id, 256) && SAFE_TOKEN.test(value.request_id) && SOURCE_REF.test(value.source_ref) && boundedText(value.last_error, 128) && SAFE_TOKEN.test(value.last_error)
}

function validateJobMetadata (job, legacy) {
  if (legacy) return true
  return boundedText(job.request_id, 256) && SAFE_TOKEN.test(job.request_id) && ['human', 'agent'].includes(job.origin) && boundedText(job.submitter, 256) && SAFE_TOKEN.test(job.submitter)
}

function validateCurationRuntime (value) {
  if (value === undefined) return true
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (value.status === 'unknown') return exactKeys(value, ['status', 'reason']) && boundedText(value.reason, 128) && SAFE_TOKEN.test(value.reason)
  if (value.status === 'started') return exactKeys(value, ['status', 'model', 'session_id']) && boundedText(value.model, 128) && boundedText(value.session_id, 256) && SAFE_TOKEN.test(value.model) && SAFE_TOKEN.test(value.session_id)
  return exactKeys(value, ['model', 'provider', 'session_id']) && boundedText(value.model, 128) && boundedText(value.provider, 128) && boundedText(value.session_id, 256) && SAFE_TOKEN.test(value.model) && SAFE_TOKEN.test(value.provider) && SAFE_TOKEN.test(value.session_id)
}

function normalizeCurationRuntime (value) {
  if (value === undefined) return undefined
  return JSON.parse(JSON.stringify(value))
}

function runtimeSameIdentity (left, right) {
  return left?.model === right?.model && left?.session_id === right?.session_id
}

function runtimeEqual (left, right) {
  return JSON.stringify(left) === JSON.stringify(right)
}

function validateJob (job) {
  if (!isRecord(job)) throw errorFor('gbrain_codex_subscription_job_invalid')
  if (!Array.isArray(job.remember_intents) || job.remember_intents.length > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || !job.remember_intents.every(validateRememberIntent)) throw errorFor('gbrain_codex_subscription_remember_intent_invalid')
  const { recovery_attempts: ignoredRecoveryAttempts, historical_reconciliation: historical, ...shapeJob } = job
  if (Object.hasOwn(job, 'historical_reconciliation') && !validateHistoricalCompletion(job, historical)) throw errorFor('gbrain_codex_subscription_historical_provenance_invalid')
  const legacy = exactKeys(shapeJob, LEGACY_JOB_KEYS) || exactKeys(shapeJob, LEGACY_JOB_KEYS_WITH_CONTEXT) || exactKeys(shapeJob, LEGACY_JOB_KEYS_WITH_ORIGINAL_TURN) || exactKeys(shapeJob, LEGACY_JOB_KEYS_WITH_ORIGINAL_TURN_WITH_CONTEXT)
  const knownShape = legacy || [JOB_KEYS, JOB_KEYS_WITH_CONTEXT, JOB_KEYS_WITHOUT_ORIGINAL_TURN, JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_CONTEXT, JOB_KEYS_WITH_RUNTIME, JOB_KEYS_WITH_CONTEXT_AND_RUNTIME, JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_RUNTIME, JOB_KEYS_WITHOUT_ORIGINAL_TURN_WITH_CONTEXT_AND_RUNTIME, JOB_KEYS_WITH_RECOVERY_OF, JOB_KEYS_WITH_CONTEXT_AND_RECOVERY_OF, JOB_KEYS_WITH_RUNTIME_AND_RECOVERY_OF, JOB_KEYS_WITH_CONTEXT_RUNTIME_AND_RECOVERY_OF, JOB_KEYS_WITH_HISTORY_RECONCILE_AND_RUNTIME_AND_RECOVERY_OF, JOB_KEYS_WITH_CONTEXT_HISTORY_RECONCILE_RUNTIME_AND_RECOVERY_OF].some((keys) => exactKeys(shapeJob, keys))
  if (Object.hasOwn(job, 'history_reconcile') && (job.history_reconcile !== true || !Object.hasOwn(job, 'recovery_of') || !job.curation_runtime || (job.curation_runtime.status !== undefined && (job.curation_runtime.status !== 'unknown' || job.curation_runtime.reason !== 'recovered_history_after_invalid_result')) || job.candidates.length !== 0 || job.candidate_hashes.length !== 0 || job.facts.length !== 0 || job.remembered.length !== 0 || job.remember_intents.length !== 0 || job.fact_refs.length !== 0)) throw errorFor('gbrain_codex_subscription_job_invalid')
  if (!knownShape || (Object.hasOwn(job, 'recovery_of') && (!validateRecoveryOf(job.recovery_of) || job.recovery_of.source_ref !== job.source_ref || job.recovery_of.job_id === job.job_id)) || job.schema !== GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA || !JOB_ID.test(job.job_id) || job.job_id !== `job-${job.input_hash}` || !validateJobMetadata(job, legacy) || !validateCurationRuntime(job.curation_runtime) || (Object.hasOwn(job, 'recovery_attempts') && (!Number.isSafeInteger(job.recovery_attempts) || job.recovery_attempts < 0 || job.recovery_attempts > 3)) || !HASH64.test(job.input_hash) || !SOURCE_REF.test(job.source_ref) || !HASH32.test(job.session_hash) || (Object.hasOwn(job, 'original_turn') && !boundedText(job.original_turn, ORIGINAL_TURN_MAX_LENGTH)) || (Object.hasOwn(job, 'prior_context') && validatePriorContext(job.prior_context, job.session_hash, job.source_ref).length === 0) || job.model !== GBRAIN_CODEX_SUBSCRIPTION_MODEL || !GBRAIN_CODEX_SUBSCRIPTION_JOB_STATUSES.includes(job.status) || !Number.isSafeInteger(job.attempts) || job.attempts < 0 || job.attempts > GBRAIN_CODEX_SUBSCRIPTION_MAX_ATTEMPTS || !Array.isArray(job.candidates) || job.candidates.length > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || !job.candidates.every((candidate) => boundedText(candidate, GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATE_LENGTH)) || !Array.isArray(job.candidate_hashes) || job.candidate_hashes.length !== job.candidates.length && job.candidates.length !== 0 || !job.candidate_hashes.every((hash) => HASH64.test(hash)) || typeof job.next_attempt_at !== 'string' || !Number.isFinite(Date.parse(job.next_attempt_at)) || typeof job.created_at !== 'string' || !Number.isFinite(Date.parse(job.created_at)) || typeof job.updated_at !== 'string' || !Number.isFinite(Date.parse(job.updated_at)) || !validateLease(job.lease) || !Array.isArray(job.facts) || job.facts.length > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || !job.facts.every((fact) => validateFact(fact) || validateRedactedFact(fact)) || !Array.isArray(job.remembered) || job.remembered.length > GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES || !job.remembered.every(validateRemembered) || !Array.isArray(job.fact_refs) || job.fact_refs.length > MAX_AUDIT_REFS || !job.fact_refs.every((ref) => FACT_REF.test(ref)) || (job.last_error !== null && (!boundedText(job.last_error, 128) || !SAFE_TOKEN.test(job.last_error))) || (job.completed_at !== null && (typeof job.completed_at !== 'string' || !Number.isFinite(Date.parse(job.completed_at)))) ) throw errorFor('gbrain_codex_subscription_job_invalid')
  if (job.status !== 'completed' && job.status !== 'failed') {
    if (job.candidate_hashes.some((hash, index) => hash !== gbrainCodexSubscriptionFactContentHash(job.candidates[index]))) throw errorFor('gbrain_codex_subscription_job_hash_mismatch')
    const hasExplicitMetadata = !legacy && (job.origin === 'agent' || job.submitter !== `source:${job.session_hash}`)
    const currentInput = { candidates: job.candidates, ...(Object.hasOwn(job, 'original_turn') ? { original_turn: job.original_turn } : {}), ...(Object.hasOwn(job, 'prior_context') ? { prior_context: job.prior_context } : {}), source_ref: job.source_ref, session_hash: job.session_hash, turn_hash: job.turn_hash, ...(hasExplicitMetadata ? { origin: job.origin, submitter: job.submitter } : {}) }
    const expectedInputHash = Object.hasOwn(job, 'recovery_of') ? computeGbrainCodexSubscriptionRecoveryInputHash({ ...currentInput, recovery_of: job.recovery_of }) : computeGbrainCodexSubscriptionInputHash(currentInput)
    if (expectedInputHash !== job.input_hash) throw errorFor('gbrain_codex_subscription_job_input_hash_mismatch')
    for (const fact of job.facts.filter((value) => value.fact !== undefined)) {
      try {
        if (gbrainCodexSubscriptionIdempotencyKey(factIdempotencyBinding(job, fact)) !== fact.idempotency_key) throw errorFor('gbrain_codex_subscription_job_fact_idempotency_mismatch')
      } catch (error) { throw error?.code === 'gbrain_codex_subscription_job_fact_idempotency_mismatch' ? error : errorFor('gbrain_codex_subscription_job_fact_idempotency_mismatch') }
    }
    for (const intent of job.remember_intents) {
      try {
        const provenance = parseGbrainCodexSubscriptionProvenance(intent.provenance)
        if (provenance === null || provenance.source_ref !== job.source_ref || provenance.input_hash !== job.input_hash || provenance.idempotency_key !== intent.idempotency_key) throw errorFor('gbrain_codex_subscription_job_intent_provenance_mismatch')
        if (gbrainCodexSubscriptionIdempotencyKey(factIdempotencyBinding(job, intent)) !== intent.idempotency_key) throw errorFor('gbrain_codex_subscription_job_intent_idempotency_mismatch')
      } catch (error) { throw error?.code === 'gbrain_codex_subscription_job_intent_idempotency_mismatch' ? error : errorFor('gbrain_codex_subscription_job_intent_idempotency_mismatch') }
    }
  }
  return job
}

function validateHistoricalCompletion (job, value) {
  return exactKeys(value, ['schema', 'execution_kind', 'historical_runtime', 'native_session_id', 'job_sha256', 'failure_raw_sha256', 'failure_ref', 'receipt_ref', 'receipt_sha256', 'audit_key']) &&
    value.schema === 'pb.pax_historical_completion.v1' && value.execution_kind === 'historical_receipt_reconciliation' &&
    exactKeys(value.historical_runtime, ['status', 'reason']) && value.historical_runtime.status === 'unknown' && value.historical_runtime.reason === 'historical_runtime_not_recorded' &&
    boundedText(value.native_session_id, 256) && /^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$/u.test(value.native_session_id) &&
    [value.job_sha256, value.failure_raw_sha256, value.receipt_sha256].every(hash => HASH64.test(hash)) &&
    value.failure_ref === `local-pax-historical-failure:${job.input_hash}` && value.receipt_ref === `local-pax-historical-receipt:${job.input_hash}` &&
    value.audit_key === `${job.input_hash}:historical-receipt:completed` &&
    job.status === 'completed' && job.origin === 'human' && Object.hasOwn(job, 'original_turn') &&
    !Object.hasOwn(job, 'curation_runtime') && !Object.hasOwn(job, 'recovery_of') && !Object.hasOwn(job, 'history_reconcile') &&
    job.last_error === null && job.lease === null && job.completed_at === job.updated_at &&
    [job.facts, job.remembered, job.remember_intents].every(items => Array.isArray(items) && items.length === 0) &&
    Array.isArray(job.fact_refs) && job.fact_refs.length > 0 && job.fact_refs.length <= 4 && Array.isArray(job.audit_refs) &&
    job.audit_refs.includes(value.failure_ref) && job.audit_refs.includes(value.receipt_ref)
}

function redactedFacts (facts) {
  return facts.filter((fact) => validateFact(fact)).map((fact) => ({
    candidate_index: fact.candidate_index,
    kind: fact.kind,
    confidence: fact.confidence,
    content_hash: fact.content_hash,
    idempotency_key: fact.idempotency_key
  }))
}

function terminalJob (job, status, nowValue, { reason = null, facts = job.facts, factRefs = job.fact_refs, auditRefs = job.audit_refs, curationRuntime } = {}) {
  const nowIso = iso(nowValue)
  return {
    ...job,
    status,
    candidates: [],
    facts: redactedFacts(facts),
    remembered: job.remembered,
    remember_intents: [],
    fact_refs: [...new Set(factRefs.filter((ref) => FACT_REF.test(ref)))].slice(0, MAX_AUDIT_REFS),
    audit_refs: [...new Set(auditRefs.filter(validateAuditRef))].slice(0, MAX_AUDIT_REFS),
    ...(curationRuntime !== undefined ? { curation_runtime: normalizeCurationRuntime(curationRuntime) } : {}),
    lease: null,
    last_error: reason,
    updated_at: nowIso,
    completed_at: nowIso
  }
}

function backoff (attempt, base, maximum) {
  return Math.min(maximum, base * (2 ** Math.max(0, attempt - 1)))
}

function retryDelay (reason, attempt, base, maximum) {
  if (reason !== PAX_CURATION_TRANSPORT_ERROR) return backoff(attempt, base, maximum)
  const delay = backoff(attempt, PAX_CURATION_TRANSPORT_BACKOFF_BASE_MS, PAX_CURATION_TRANSPORT_BACKOFF_MAX_MS)
  return Math.min(PAX_CURATION_TRANSPORT_BACKOFF_MAX_MS, Math.floor(delay * (0.75 + Math.random() * 0.5)))
}

function sleep (milliseconds) {
  return new Promise((resolveSleep) => {
    const timer = setTimeout(resolveSleep, milliseconds)
    timer.unref?.()
  })
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
    if (!isRecord(value) || value.schema !== GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA || !boundedText(value.owner, 256) || !SAFE_TOKEN.test(value.owner) || !Number.isSafeInteger(value.pid) || value.pid < 1) return null
    return value
  } catch {
    return null
  }
}

async function recoverStaleLock ({ fsImpl, lockPath, observed, lockTtlMs, lockNow, isProcessAlive, directory }) {
  if (!Number.isFinite(observed?.mtimeMs) || lockNow() - observed.mtimeMs <= lockTtlMs) return false
  const owner = await lockOwner(fsImpl, lockPath)
  if (!owner) {
    // An interrupted first write can leave a zero-byte lock with no owner.
    // Only reclaim that unambiguously incomplete lock after rechecking the
    // exact inode, size, and mtime observed above.
    if (observed.size !== 0) return false
    const confirmed = await fsImpl.lstat(lockPath).catch((error) => {
      if (error?.code === 'ENOENT') return null
      throw error
    })
    if (!confirmed || !sameLockObject(observed, confirmed)) return false
    await fsImpl.unlink(lockPath)
    await syncDirectory(fsImpl, directory)
    return true
  }
  if (owner.pid === process.pid || isProcessAlive(owner.pid)) return false
  const ownerAfterRead = await lockOwner(fsImpl, lockPath)
  if (!ownerAfterRead || ownerAfterRead.owner !== owner.owner || ownerAfterRead.pid !== owner.pid) return false
  const confirmed = await fsImpl.lstat(lockPath).catch((error) => {
    if (error?.code === 'ENOENT') return null
    throw error
  })
  if (!confirmed || !sameLockObject(observed, confirmed)) return false
  await fsImpl.unlink(lockPath)
  await syncDirectory(fsImpl, directory)
  return true
}

export function createGbrainCodexSubscriptionQueue ({
  stateDir = DEFAULT_GBRAIN_CODEX_SUBSCRIPTION_QUEUE_STATE_DIR,
  sourceId = 'business-pb',
  auditLedger = null,
  historicalReceipt = null,
  fsImpl = fs,
  isProcessAlive = defaultProcessAlive,
  lockNow = () => Date.now(),
  now = () => Date.now(),
  lockTtlMs = 30_000,
  lockWaitMs = 5_000,
  maxAttempts = GBRAIN_CODEX_SUBSCRIPTION_MAX_ATTEMPTS,
  backoffBaseMs = 1_000,
  backoffMaxMs = 60_000,
  leaseMs = GBRAIN_CODEX_SUBSCRIPTION_DEFAULT_LEASE_MS,
  tokenFactory = () => uniqueToken('owner')
} = {}) {
  if (typeof stateDir !== 'string' || stateDir.length === 0 || stateDir.includes('\0')) throw errorFor('gbrain_codex_subscription_state_dir_invalid')
  if (typeof sourceId !== 'string' || !/^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/u.test(sourceId)) throw errorFor('gbrain_codex_subscription_source_id_invalid')
  if (![lockTtlMs, lockWaitMs, backoffBaseMs, backoffMaxMs, leaseMs].every((value) => Number.isSafeInteger(value) && value > 0)) throw errorFor('gbrain_codex_subscription_timing_invalid')
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 8) throw errorFor('gbrain_codex_subscription_max_attempts_invalid')
  if (typeof tokenFactory !== 'function') throw errorFor('gbrain_codex_subscription_token_factory_invalid')
  if (!fsImpl || typeof fsImpl !== 'object') throw errorFor('gbrain_codex_subscription_filesystem_invalid')
  if (typeof isProcessAlive !== 'function' || typeof lockNow !== 'function') throw errorFor('gbrain_codex_subscription_lock_seam_invalid')
  const absoluteStateDir = resolve(stateDir)
  const jobsDir = join(absoluteStateDir, 'jobs')
  const lockPath = join(absoluteStateDir, '.queue.lock')

  async function ensureState () {
    await ensureDirectory(fsImpl, absoluteStateDir, { recursive: true })
    await ensureDirectory(fsImpl, jobsDir)
  }

  async function acquireLock ({ path = lockPath, directory = absoluteStateDir, prepare = ensureState } = {}) {
    await prepare()
    const started = lockNow()
    while (lockNow() - started <= lockWaitMs) {
      let handle
      try {
        handle = await fsImpl.open(path, LOCK_FLAGS, QUEUE_FILE_MODE)
        const info = await handle.stat()
        validateFileInfo(info)
        const owner = tokenFactory()
        if (!boundedText(owner, 256) || !SAFE_TOKEN.test(owner)) throw errorFor('gbrain_codex_subscription_lock_owner_invalid')
        await handle.write(JSON.stringify({ schema: GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA, owner, pid: process.pid }), null, 'utf8')
        await handle.sync()
        const lockInfo = await handle.stat()
        validateFileInfo(lockInfo)
        return {
          handle,
          info: lockInfo,
          owner,
          async release () {
            try {
              const current = await fsImpl.lstat(path)
              if (!sameLockObject(lockInfo, current)) return
              const confirmed = await fsImpl.lstat(path)
              if (!sameLockObject(lockInfo, confirmed)) return
              await fsImpl.unlink(path)
              await syncDirectory(fsImpl, directory)
            } finally {
              await handle.close().catch(() => {})
            }
          }
        }
      } catch (error) {
        await handle?.close().catch(() => {})
        if (error?.code !== 'EEXIST') throw error
        let info
        try { info = await fsImpl.lstat(path) } catch (lstatError) {
          if (lstatError?.code === 'ENOENT') continue
          throw lstatError
        }
        validateFileInfo(info)
        if (await recoverStaleLock({ fsImpl, lockPath: path, observed: info, lockTtlMs, lockNow, isProcessAlive, directory })) continue
        await sleep(5)
      }
    }
    throw errorFor('gbrain_codex_subscription_lock_timeout')
  }

  async function withLock (operation) {
    const lock = await acquireLock()
    try { return await operation() } finally { await lock.release() }
  }

  async function readAllJobs () {
    await ensureState()
    const names = await fsImpl.readdir(jobsDir)
    const jobs = []
    for (const name of names) {
      if (!JOB_FILE.test(name)) {
        if (TEMP_FILE.test(name)) {
          validateFileInfo(await fsImpl.lstat(join(jobsDir, name)))
          continue
        }
        throw errorFor('gbrain_codex_subscription_filename_invalid')
      }
      const job = validateJob(await readJsonFile(fsImpl, join(jobsDir, name)))
      if (`${job.job_id}.json` !== name) throw errorFor('gbrain_codex_subscription_filename_mismatch')
      jobs.push(job)
    }
    jobs.sort((left, right) => left.created_at.localeCompare(right.created_at) || left.job_id.localeCompare(right.job_id))
    return jobs
  }

  function jobPath (jobId) {
    if (!JOB_ID.test(jobId)) throw errorFor('gbrain_codex_subscription_job_id_invalid')
    return join(jobsDir, `${jobId}.json`)
  }

  async function readJob (jobId) {
    await ensureState()
    return validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
  }

  function expectedFailedIdentityMatches (job, expected) {
    const requestId = job.request_id || `pax-curation:${job.input_hash}`
    return job.status === 'failed' && requestId === expected.requestId && job.source_ref === expected.sourceRef && job.input_hash === expected.inputHash && job.last_error === expected.lastError
  }

  function recoveryOfFor (job) {
    return {
      job_id: job.job_id,
      input_hash: job.input_hash,
      request_id: job.request_id || `pax-curation:${job.input_hash}`,
      source_ref: job.source_ref,
      last_error: job.last_error
    }
  }

  function recoveryInputFor (job, recoveryOf = recoveryOfFor(job)) {
    const hasExplicitMetadata = job.origin === 'agent' || job.submitter !== `source:${job.session_hash}`
    return validateInput({
      candidates: [],
      original_turn: job.original_turn,
      ...(Object.hasOwn(job, 'prior_context') ? { prior_context: job.prior_context } : {}),
      source_ref: job.source_ref,
      session_hash: job.session_hash,
      turn_hash: job.turn_hash,
      ...(hasExplicitMetadata ? { origin: job.origin || 'human', submitter: job.submitter || `source:${job.session_hash}` } : {}),
      recovery_of: recoveryOf
    }, { allowRecovery: true })
  }

  function hasNoLocalOutcomes (job) {
    return job.facts.length === 0 && job.fact_refs.length === 0 && job.remembered.length === 0 && job.remember_intents.length === 0
  }

  async function validateRecoveryParent (job) {
    if (!Object.hasOwn(job, 'recovery_of')) return null
    const recoveryOf = job.recovery_of
    if (!validateRecoveryOf(recoveryOf) || recoveryOf.source_ref !== job.source_ref) return false
    let parent
    try {
      parent = validateJob(await readJsonFile(fsImpl, jobPath(recoveryOf.job_id)))
    } catch {
      return false
    }
    if (Object.hasOwn(parent, 'recovery_of') || parent.job_id !== recoveryOf.job_id || parent.status !== 'failed' || !Object.hasOwn(parent, 'original_turn') || !hasNoLocalOutcomes(parent) || !hasNoLocalOutcomes(job)) return false
    if (parent.input_hash !== recoveryOf.input_hash || (parent.request_id || `pax-curation:${parent.input_hash}`) !== recoveryOf.request_id || parent.source_ref !== recoveryOf.source_ref || parent.last_error !== recoveryOf.last_error || parent.source_ref !== job.source_ref) return false
    return parent
  }

  async function enqueueRecoveryFromFailed ({ jobId, expectedRequestId, expectedSourceRef, expectedInputHash, expectedLastError } = {}) {
    const expected = { requestId: expectedRequestId, sourceRef: expectedSourceRef, inputHash: expectedInputHash, lastError: expectedLastError }
    if (!JOB_ID.test(jobId) || !boundedText(expectedRequestId, 256) || !SAFE_TOKEN.test(expectedRequestId) || !SOURCE_REF.test(expectedSourceRef) || !HASH64.test(expectedInputHash) || !boundedText(expectedLastError, 128) || !SAFE_TOKEN.test(expectedLastError)) throw errorFor('gbrain_codex_subscription_recovery_invalid')
    const observed = await readJob(jobId)
    if (!expectedFailedIdentityMatches(observed, expected) || !Object.hasOwn(observed, 'original_turn')) return queueResult('fenced', null)
    if (Object.hasOwn(observed, 'recovery_of') && !(await validateRecoveryParent(observed))) return queueResult('fenced', null)
    const input = recoveryInputFor(observed)
    const inputHash = computeGbrainCodexSubscriptionRecoveryInputHash(input)
    if (process.env[GBRAIN_CODEX_SUBSCRIPTION_KILL_SWITCH] !== '1') return queueResult('disabled', inputHash)
    if (!hasNoLocalOutcomes(observed)) return queueResult('fenced', inputHash)
    try {
      await recordGbrainCodexSubscriptionAudit({ auditLedger, input, operation: 'codex_subscription_enqueue', phase: 'attempted', idempotencyKey: `${inputHash}:enqueue:attempted` })
    } catch {
      return queueResult('unavailable', inputHash, { reason: 'enqueue_audit_unavailable' })
    }

    try {
      return await withLock(async () => {
        const oldJob = validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
        if (!expectedFailedIdentityMatches(oldJob, expected) || !Object.hasOwn(oldJob, 'original_turn') || !hasNoLocalOutcomes(oldJob)) return queueResult('fenced', inputHash)
        const parent = await validateRecoveryParent(oldJob)
        if (parent === false) return queueResult('fenced', inputHash)
        const currentInput = recoveryInputFor(oldJob)
        if (computeGbrainCodexSubscriptionRecoveryInputHash(currentInput) !== inputHash) return queueResult('fenced', inputHash)
        const recoveryOf = currentInput.recovery_of
        const newJobId = `job-${inputHash}`
        const existingPath = jobPath(newJobId)
        try {
          validateFileInfo(await fsImpl.lstat(existingPath))
          const existing = validateJob(await readJsonFile(fsImpl, existingPath))
          if (!existing.recovery_of || gbrainCodexSubscriptionCanonicalJson(existing.recovery_of) !== gbrainCodexSubscriptionCanonicalJson(recoveryOf) || existing.source_ref !== currentInput.source_ref || existing.input_hash !== inputHash) return queueResult('conflict', inputHash, { job_id: existing.job_id })
          const durable = await acknowledgeEnqueue(existing, currentInput)
          await recordGbrainCodexSubscriptionAudit({ auditLedger, input: currentInput, operation: 'codex_subscription_enqueue', phase: 'completed', idempotencyKey: `${inputHash}:enqueue:duplicate:completed`, resultRef: `local-csx-queue:${inputHash}`, resultCount: 0 })
          return queueResult('duplicate', inputHash, { job_id: durable.job_id, request_id: durable.request_id })
        } catch (error) {
          if (error?.code !== 'ENOENT') throw error
        }
        const sameSourceJobs = (await readAllJobs()).filter((job) => job.source_ref === currentInput.source_ref)
        const allowedSourceJobIds = parent ? new Set([oldJob.job_id, parent.job_id]) : new Set([oldJob.job_id])
        if (sameSourceJobs.some((job) => !allowedSourceJobIds.has(job.job_id)) || sameSourceJobs.length !== allowedSourceJobIds.size) {
          const sameSource = sameSourceJobs.find((job) => !allowedSourceJobIds.has(job.job_id)) || sameSourceJobs.find((job) => job.job_id !== oldJob.job_id)
          return queueResult('conflict', inputHash, { existing_input_hash: sameSource?.input_hash })
        }
        const createdAt = iso(now)
        const job = {
          schema: GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA,
          job_id: newJobId,
          request_id: `pax-curation:${inputHash}`,
          origin: input.origin || 'human',
          submitter: input.submitter || `source:${input.session_hash}`,
          input_hash: inputHash,
          original_turn: input.original_turn,
          ...(Object.hasOwn(input, 'prior_context') ? { prior_context: structuredClone(input.prior_context) } : {}),
          source_ref: input.source_ref,
          session_hash: input.session_hash,
          turn_hash: input.turn_hash,
          model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
          candidates: [],
          candidate_hashes: [],
          status: 'queued',
          attempts: 0,
          next_attempt_at: createdAt,
          created_at: createdAt,
          updated_at: createdAt,
          lease: null,
          facts: [],
          remembered: [],
          remember_intents: [],
          fact_refs: [],
          audit_refs: [],
          last_error: null,
          completed_at: null,
          recovery_of: recoveryOf
        }
        validateJob(job)
        const path = jobPath(job.job_id)
        await atomicWriteJson(path, job, { replace: false, tokenFactory, fsImpl })
        const acknowledged = await acknowledgeEnqueue(job, currentInput)
        return queueResult('queued', inputHash, { job_id: acknowledged.job_id, request_id: job.request_id, recovery_of: oldJob.job_id })
      })
    } catch (error) {
      let failedAuditError = null
      if (!(await durableJobExists(inputHash))) {
        try {
          await recordGbrainCodexSubscriptionAudit({ auditLedger, input, operation: 'codex_subscription_enqueue', phase: 'failed', idempotencyKey: `${inputHash}:enqueue:failed`, errorCode: 'queue_unavailable' })
        } catch (auditError) {
          failedAuditError = auditError
        }
      }
      return queueResult('unavailable', inputHash, { reason: error?.code === 'gbrain_codex_subscription_audit_required' ? 'enqueue_audit_unavailable' : 'queue_unavailable', ...(failedAuditError ? { audit_status: 'unavailable' } : {}) })
    }
  }

  // Originals complete atomically from readback; derived jobs retain their receipt-only worker path.
  async function reconcileFailedRecoveryFromHistory ({ jobId, expectedRequestId, expectedSourceRef, expectedInputHash, expectedLastError, expectedRecoveryOf, callVerb } = {}) {
    const originalTimeout = expectedRecoveryOf === undefined && expectedLastError === 'gbrain_shadow_timeout'
    const invalidModelResult = expectedLastError === 'pax_memory_curation_result_invalid'
    if (!JOB_ID.test(jobId) || !boundedText(expectedRequestId, 256) || !SAFE_TOKEN.test(expectedRequestId) || !SOURCE_REF.test(expectedSourceRef) || !HASH64.test(expectedInputHash) || (!originalTimeout && ((!invalidModelResult && expectedLastError !== 'gbrain_codex_subscription_curation_receipt_unverified') || !validateRecoveryOf(expectedRecoveryOf)))) throw errorFor('gbrain_codex_subscription_recovery_reconcile_invalid')
    if (process.env[GBRAIN_CODEX_SUBSCRIPTION_KILL_SWITCH] !== '1') return queueResult('disabled', expectedInputHash)
    const noOutcomes = (job) => job.facts.length === 0 && job.fact_refs.length === 0 && job.remembered.length === 0 && job.remember_intents.length === 0
    const matchesLineage = (job) => job.job_id === jobId && job.request_id === expectedRequestId && job.source_ref === expectedSourceRef && job.input_hash === expectedInputHash && (originalTimeout ? !Object.hasOwn(job, 'recovery_of') : Object.hasOwn(job, 'recovery_of') && gbrainCodexSubscriptionCanonicalJson(job.recovery_of) === gbrainCodexSubscriptionCanonicalJson(expectedRecoveryOf))

    if (originalTimeout) {
      if (typeof callVerb !== 'function') throw errorFor('gbrain_codex_subscription_history_readback_required')
      const observed = validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
      if (!matchesLineage(observed) || !Object.hasOwn(observed, 'original_turn') || observed.candidates.length !== 0 || observed.candidate_hashes.length !== 0 || !noOutcomes(observed) || !observed.curation_runtime || observed.curation_runtime.status !== undefined) return queueResult('fenced', expectedInputHash)
      const auditRef = `local-pax-curation:${observed.input_hash}`
      if (observed.status === 'completed' && observed.audit_refs.includes(auditRef)) return queueResult('duplicate', expectedInputHash, { job_id: observed.job_id, request_id: observed.request_id })
      if (observed.status !== 'failed' || observed.last_error !== expectedLastError) return queueResult('fenced', expectedInputHash)
      const response = await callVerb('curation_history', { limit: 100, request_id: expectedRequestId })
      if (!isRecord(response) || !Array.isArray(response.items) || response.items.length > 100 || (response.next_cursor !== null && typeof response.next_cursor !== 'string')) throw errorFor('gbrain_codex_subscription_history_readback_invalid')
      const history = response.items.find((row) => isRecord(row) && row.request_id === expectedRequestId)
      const exactNoChange = history?.pax_run === expectedRequestId && Array.isArray(history.decisions) && history.decisions.length > 0 && history.decisions.length <= 4 && history.decisions.every((decision) => isRecord(decision) && decision.action === 'no_change' && Array.isArray(decision.old_fact_ids) && decision.old_fact_ids.length === 0 && decision.new_fact_id === null && decision.visibility === 'world' && boundedText(decision.reason, 2_000) && Array.isArray(decision.source_refs) && decision.source_refs.includes(expectedSourceRef) && decision.source_refs.every((ref) => typeof ref === 'string' && /^[^\x00-\x1f\x7f]{1,512}$/u.test(ref)))
      if (!exactNoChange) return queueResult('fenced', expectedInputHash)
      return withLock(async () => {
        const job = validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
        if (gbrainCodexSubscriptionCanonicalJson(job) !== gbrainCodexSubscriptionCanonicalJson(observed)) return queueResult('fenced', expectedInputHash)
        await recordGbrainCodexSubscriptionAudit({ auditLedger, input: inputFromJob(job), operation: 'codex_subscription_curate', phase: 'completed', idempotencyKey: `${job.input_hash}:curate:${Math.max(1, job.attempts)}:completed`, resultRef: auditRef, resultCount: history.decisions.length })
        const completed = terminalJob(job, 'completed', now, { auditRefs: [...job.audit_refs, auditRef] })
        validateJob(completed)
        await atomicWriteJson(jobPath(job.job_id), completed, { replace: true, tokenFactory, fsImpl })
        return queueResult('completed', expectedInputHash, { job_id: job.job_id, request_id: job.request_id })
      })
    }

    return withLock(async () => {
      const job = validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
      if (!matchesLineage(job) || !Object.hasOwn(job, 'original_turn') || job.candidates.length !== 0 || job.candidate_hashes.length !== 0 || !noOutcomes(job) || !job.curation_runtime) return queueResult('fenced', expectedInputHash)
      if (job.status === 'retry' && job.history_reconcile === true) return queueResult('duplicate', expectedInputHash, { job_id: job.job_id, request_id: job.request_id })
      if (job.status === 'completed' && job.history_reconcile === true) return queueResult('duplicate', expectedInputHash, { job_id: job.job_id, request_id: job.request_id })
      const markedRetry = invalidModelResult && job.history_reconcile === true && job.curation_runtime.status === 'unknown' && job.curation_runtime.reason === 'recovered_history_after_invalid_result'
      const runtimeMatchesFailure = invalidModelResult ? job.curation_runtime.status === 'started' || markedRetry : job.curation_runtime.status === undefined
      if (!runtimeMatchesFailure) return queueResult('fenced', expectedInputHash)
      if (job.status !== 'failed' || (job.history_reconcile === true && !markedRetry) || job.last_error !== expectedLastError || (Object.hasOwn(job, 'recovery_attempts') && job.recovery_attempts >= 3)) return queueResult('fenced', expectedInputHash)

      const oldIdentity = { requestId: expectedRecoveryOf.request_id, sourceRef: expectedRecoveryOf.source_ref, inputHash: expectedRecoveryOf.input_hash, lastError: expectedRecoveryOf.last_error }
      const original = validateJob(await readJsonFile(fsImpl, jobPath(expectedRecoveryOf.job_id)))
      if (!expectedFailedIdentityMatches(original, oldIdentity) || !Object.hasOwn(original, 'original_turn') || original.facts.length !== 0 || original.fact_refs.length !== 0 || original.remembered.length !== 0 || original.remember_intents.length !== 0) return queueResult('fenced', expectedInputHash)
      const reboundRecoveryInput = recoveryInputFor(original)
      if (computeGbrainCodexSubscriptionRecoveryInputHash(reboundRecoveryInput) !== job.input_hash) return queueResult('fenced', expectedInputHash)

      const currentMs = nowMilliseconds(now)
      const reconciled = { ...job, status: 'retry', history_reconcile: true, ...(invalidModelResult ? { curation_runtime: { status: 'unknown', reason: 'recovered_history_after_invalid_result' } } : {}), recovery_attempts: job.recovery_attempts || 0, lease: null, next_attempt_at: iso(currentMs), updated_at: iso(currentMs), completed_at: null }
      validateJob(reconciled)
      await atomicWriteJson(jobPath(job.job_id), reconciled, { replace: true, tokenFactory, fsImpl })
      return queueResult('requeued', expectedInputHash, { job_id: job.job_id, request_id: job.request_id })
    })
  }

  function jobMatchesInput (job, input, inputHash) {
    const recoveryMatches = Object.hasOwn(job, 'recovery_of')
      ? Object.hasOwn(input, 'recovery_of') && gbrainCodexSubscriptionCanonicalJson(job.recovery_of) === gbrainCodexSubscriptionCanonicalJson(input.recovery_of)
      : !Object.hasOwn(input, 'recovery_of')
    return recoveryMatches && job.input_hash === inputHash && job.source_ref === input.source_ref && job.session_hash === input.session_hash && job.turn_hash === input.turn_hash && (job.origin || 'human') === (input.origin || 'human') && (job.submitter || `source:${input.session_hash}`) === (input.submitter || `source:${input.session_hash}`) && (job.candidates.length === 0 || gbrainCodexSubscriptionCanonicalJson(job.candidates) === gbrainCodexSubscriptionCanonicalJson(input.candidates))
  }

  async function durableJobExists (inputHash) {
    try {
      validateFileInfo(await fsImpl.lstat(jobPath(`job-${inputHash}`)))
      return true
    } catch (error) {
      if (error?.code === 'ENOENT') return false
      return true
    }
  }

  async function acknowledgeEnqueue (job, input, currentNow = now) {
    const auditRef = enqueueAuditRef(job.input_hash)
    if (job.audit_refs.includes(auditRef)) return job
    await recordGbrainCodexSubscriptionAudit({
      auditLedger,
      input,
      operation: 'codex_subscription_enqueue',
      phase: 'completed',
      idempotencyKey: `${job.input_hash}:enqueue:queued:completed`,
      resultRef: auditRef,
      resultCount: 1
    })
    const path = jobPath(job.job_id)
    const durable = validateJob(await readJsonFile(fsImpl, path))
    if (!jobMatchesInput(durable, input, job.input_hash)) throw errorFor('gbrain_codex_subscription_enqueue_reconciliation_mismatch')
    if (durable.audit_refs.includes(auditRef)) return durable
    const acknowledged = {
      ...durable,
      audit_refs: [...new Set([auditRef, ...durable.audit_refs])].slice(0, MAX_AUDIT_REFS),
      updated_at: iso(currentNow)
    }
    validateJob(acknowledged)
    await atomicWriteJson(path, acknowledged, { replace: true, tokenFactory, fsImpl })
    const verified = validateJob(await readJsonFile(fsImpl, path))
    if (!verified.audit_refs.includes(auditRef)) throw errorFor('gbrain_codex_subscription_enqueue_ack_missing')
    return verified
  }

  function inputFromJob (job) {
    return {
      candidates: [...job.candidates],
      ...(Object.hasOwn(job, 'original_turn') ? { original_turn: job.original_turn } : {}),
      ...(Object.hasOwn(job, 'prior_context') ? { prior_context: structuredClone(job.prior_context) } : {}),
      source_ref: job.source_ref,
      session_hash: job.session_hash,
      turn_hash: job.turn_hash,
      ...(job.origin === 'agent' || job.submitter !== `source:${job.session_hash}` ? { origin: job.origin || 'human', submitter: job.submitter || `source:${job.session_hash}` } : {}),
      ...(Object.hasOwn(job, 'recovery_of') ? { recovery_of: structuredClone(job.recovery_of) } : {})
    }
  }

  async function enqueue (value) {
    let input
    try { input = validateInput(value) } catch { return queueResult('unavailable', null, { reason: 'invalid_input' }) }
    const inputHash = computeGbrainCodexSubscriptionInputHash(input)
    if (process.env[GBRAIN_CODEX_SUBSCRIPTION_KILL_SWITCH] !== '1') return queueResult('disabled', inputHash)
    try {
      await recordGbrainCodexSubscriptionAudit({ auditLedger, input, operation: 'codex_subscription_enqueue', phase: 'attempted', idempotencyKey: `${inputHash}:enqueue:attempted` })
    } catch {
      return queueResult('unavailable', inputHash, { reason: 'enqueue_audit_unavailable' })
    }
    try {
      const result = await withLock(async () => {
        const jobs = await readAllJobs()
        const sameSource = jobs.find((job) => job.source_ref === input.source_ref)
        if (sameSource && sameSource.input_hash !== inputHash) return queueResult('conflict', inputHash, { existing_input_hash: sameSource.input_hash })
        if (sameSource) {
          await syncDirectory(fsImpl, jobsDir)
          const durable = validateJob(await readJsonFile(fsImpl, jobPath(sameSource.job_id)))
          if (!jobMatchesInput(durable, input, inputHash)) throw errorFor('gbrain_codex_subscription_duplicate_input_mismatch')
          const acknowledged = durable.audit_refs.includes(enqueueAuditRef(inputHash))
          await acknowledgeEnqueue(durable, input)
          if (!acknowledged) return queueResult('duplicate', inputHash, { job_id: durable.job_id, request_id: durable.request_id })
          await recordGbrainCodexSubscriptionAudit({ auditLedger, input, operation: 'codex_subscription_enqueue', phase: 'completed', idempotencyKey: `${inputHash}:enqueue:duplicate:completed`, resultRef: `local-csx-queue:${inputHash}`, resultCount: 0 })
          return queueResult('duplicate', inputHash, { job_id: durable.job_id, request_id: durable.request_id })
        }
        const createdAt = iso(now)
        const job = {
          schema: GBRAIN_CODEX_SUBSCRIPTION_QUEUE_SCHEMA,
          job_id: `job-${inputHash}`,
          request_id: `pax-curation:${inputHash}`,
          origin: input.origin || 'human',
          submitter: input.submitter || `source:${input.session_hash}`,
          input_hash: inputHash,
          ...(Object.hasOwn(input, 'original_turn') ? { original_turn: input.original_turn } : {}),
          ...(Object.hasOwn(input, 'prior_context') ? { prior_context: structuredClone(input.prior_context) } : {}),
          source_ref: input.source_ref,
          session_hash: input.session_hash,
          turn_hash: input.turn_hash,
          model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
          candidates: [...input.candidates],
          candidate_hashes: input.candidates.map((candidate) => gbrainCodexSubscriptionFactContentHash(candidate)),
          status: 'queued',
          attempts: 0,
          next_attempt_at: createdAt,
          created_at: createdAt,
          updated_at: createdAt,
          lease: null,
          facts: [],
          remembered: [],
          remember_intents: [],
          fact_refs: [],
          audit_refs: [],
          last_error: null,
          completed_at: null
        }
        validateJob(job)
        const path = jobPath(job.job_id)
        await atomicWriteJson(path, job, { replace: false, tokenFactory, fsImpl })
        const acknowledged = await acknowledgeEnqueue(job, input)
        return queueResult('queued', inputHash, { job_id: acknowledged.job_id, request_id: job.request_id })
      })
      return result
    } catch (error) {
      let failedAuditError = null
      if (!(await durableJobExists(inputHash))) {
        try {
          await recordGbrainCodexSubscriptionAudit({ auditLedger, input, operation: 'codex_subscription_enqueue', phase: 'failed', idempotencyKey: `${inputHash}:enqueue:failed`, errorCode: 'queue_unavailable' })
        } catch (auditError) {
          failedAuditError = auditError
        }
      }
      return queueResult('unavailable', inputHash, { reason: error?.code === 'gbrain_codex_subscription_audit_required' ? 'enqueue_audit_unavailable' : 'queue_unavailable', ...(failedAuditError ? { audit_status: 'unavailable' } : {}) })
    }
  }

  async function claimNext ({ ownerId = tokenFactory(), now: requestedNow } = {}) {
    if (!boundedText(ownerId, 256) || !SAFE_TOKEN.test(ownerId)) throw errorFor('gbrain_codex_subscription_owner_invalid')
    const currentMs = nowMilliseconds(requestedNow === undefined ? now : requestedNow)
    return withLock(async () => {
      const jobs = await readAllJobs()
      if (jobs.some((job) => job.status === 'running' && Number.isFinite(Date.parse(job.lease?.expires_at || '')) && Date.parse(job.lease.expires_at) > currentMs)) return null
      const claimOrder = [...jobs.filter((job) => !Object.hasOwn(job, 'recovery_of')), ...jobs.filter((job) => Object.hasOwn(job, 'recovery_of'))]
      for (const original of claimOrder) {
        let job = original
        let recoveryOnly = false
        if (job.status === 'running') {
          const expires = Date.parse(job.lease?.expires_at || '')
          if (!Number.isFinite(expires) || expires > currentMs) continue
          if (!job.audit_refs.includes(enqueueAuditRef(job.input_hash))) {
            try { job = await acknowledgeEnqueue(job, inputFromJob(job), currentMs) } catch { continue }
          }
          if (job.status !== 'running') continue
          if (job.attempts >= maxAttempts) {
            recoveryOnly = true
          }
          job = { ...job, status: 'retry', lease: null, next_attempt_at: iso(currentMs), updated_at: iso(currentMs) }
        }
        if (job.status === 'retry' && Object.hasOwn(job, 'recovery_attempts')) {
          recoveryOnly = true
          if (job.recovery_attempts >= 3) {
            await atomicWriteJson(jobPath(job.job_id), terminalJob(job, 'failed', currentMs, { reason: job.last_error || 'recovery_unverified' }), { replace: true, tokenFactory, fsImpl })
            continue
          }
          job = { ...job, recovery_attempts: job.recovery_attempts + 1 }
        } else if (recoveryOnly) {
          job = { ...job, recovery_attempts: 1 }
        }
        if (!['queued', 'retry'].includes(job.status) || Date.parse(job.next_attempt_at) > currentMs) continue
        if (!job.audit_refs.includes(enqueueAuditRef(job.input_hash))) {
          try { job = await acknowledgeEnqueue(job, inputFromJob(job), currentMs) } catch { continue }
          if (!['queued', 'retry'].includes(job.status) || Date.parse(job.next_attempt_at) > currentMs) continue
        }
        if (job.attempts >= maxAttempts && !recoveryOnly) {
          await atomicWriteJson(jobPath(job.job_id), terminalJob(job, 'failed', currentMs, { reason: 'max_attempts' }), { replace: true, tokenFactory, fsImpl })
          continue
        }
        const token = tokenFactory()
        if (!boundedText(token, 256) || !SAFE_TOKEN.test(token)) throw errorFor('gbrain_codex_subscription_lease_token_invalid')
        const claimed = {
          ...job,
          status: 'running',
          attempts: recoveryOnly ? job.attempts : job.attempts + 1,
          lease: { owner_id: ownerId, token, expires_at: iso(currentMs + leaseMs) },
          updated_at: iso(currentMs)
        }
        validateJob(claimed)
        await atomicWriteJson(jobPath(claimed.job_id), claimed, { replace: true, tokenFactory, fsImpl })
        return recoveryOnly ? { ...structuredClone(claimed), recovery_only: true } : structuredClone(claimed)
      }
      return null
    })
  }

  async function leaseOwned (jobId, ownerId, leaseToken) {
    try {
      if (!boundedText(ownerId, 256) || !SAFE_TOKEN.test(ownerId) || !boundedText(leaseToken, 256) || !SAFE_TOKEN.test(leaseToken)) return false
      const job = await readJob(jobId)
      return job.status === 'running' && job.lease?.owner_id === ownerId && job.lease?.token === leaseToken && Date.parse(job.lease.expires_at) > nowMilliseconds(now)
    } catch {
      return false
    }
  }

  async function renewLease ({ jobId, ownerId, leaseToken, now: requestedNow } = {}) {
    const currentMs = nowMilliseconds(requestedNow === undefined ? now : requestedNow)
    return withLock(async () => {
      const job = validateJob(await readJsonFile(fsImpl, jobPath(jobId)))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return false
      const renewed = { ...job, lease: { ...job.lease, expires_at: iso(currentMs + leaseMs) }, updated_at: iso(currentMs) }
      await atomicWriteJson(jobPath(jobId), renewed, { replace: true, tokenFactory, fsImpl })
      return true
    })
  }

  async function recordCurationRuntime ({ jobId, ownerId, leaseToken, requestId, runtime } = {}) {
    if (!boundedText(ownerId, 256) || !SAFE_TOKEN.test(ownerId) || !boundedText(leaseToken, 256) || !SAFE_TOKEN.test(leaseToken) || !boundedText(requestId, 256) || !SAFE_TOKEN.test(requestId) || !validateCurationRuntime(runtime)) throw errorFor('gbrain_codex_subscription_runtime_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      if (job.request_id !== requestId) throw errorFor('gbrain_codex_subscription_runtime_request_mismatch')
      const current = job.curation_runtime
      if (current && current.status !== 'unknown') {
        if (!runtimeSameIdentity(current, runtime)) throw errorFor('gbrain_codex_subscription_runtime_conflict')
        if (runtimeEqual(current, runtime)) return Object.freeze({ status: 'duplicate', curation_runtime: structuredClone(current) })
        if (current.status !== 'started' || runtime.status !== undefined) throw errorFor('gbrain_codex_subscription_runtime_conflict')
      }
      if (runtime.status !== 'started' && (!current || current.status !== 'started')) throw errorFor('gbrain_codex_subscription_runtime_precondition_missing')
      const updated = { ...job, curation_runtime: normalizeCurationRuntime(runtime), updated_at: iso(now) }
      validateJob(updated)
      await atomicWriteJson(path, updated, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'recorded', curation_runtime: structuredClone(runtime) })
    })
  }

  async function resetStartedCurationRuntime ({ jobId, ownerId, leaseToken } = {}) {
    if (!boundedText(ownerId, 256) || !SAFE_TOKEN.test(ownerId) || !boundedText(leaseToken, 256) || !SAFE_TOKEN.test(leaseToken)) throw errorFor('gbrain_codex_subscription_runtime_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      if (job.curation_runtime?.status !== 'started') return Object.freeze({ status: 'unchanged' })
      const { curation_runtime, ...withoutRuntime } = job
      const updated = { ...withoutRuntime, updated_at: iso(now) }
      validateJob(updated)
      await atomicWriteJson(path, updated, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'reset' })
    })
  }

  async function prepareRemember ({ jobId, ownerId, leaseToken, intent } = {}) {
    const pending = { ...intent, status: 'pending', fact_refs: [] }
    if (!validateRememberIntent(pending)) throw errorFor('gbrain_codex_subscription_remember_intent_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      const existing = job.remember_intents.find((item) => item.idempotency_key === pending.idempotency_key)
      if (existing) {
        const same = existing.fact === pending.fact && existing.provenance === pending.provenance && existing.entity === pending.entity && existing.kind === pending.kind && existing.confidence === pending.confidence && existing.visibility === pending.visibility && existing.content_hash === pending.content_hash
        if (!same) throw errorFor('gbrain_codex_subscription_remember_intent_conflict')
        return Object.freeze({ status: existing.status, intent: structuredClone(existing) })
      }
      const updated = { ...job, remember_intents: [...job.remember_intents, pending], updated_at: iso(now) }
      validateJob(updated)
      await atomicWriteJson(path, updated, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'recorded', intent: structuredClone(pending) })
    })
  }

  async function recordRemembered ({ jobId, ownerId, leaseToken, candidateIndex, idempotencyKey, factRefs, status } = {}) {
    if (!Number.isSafeInteger(candidateIndex) || candidateIndex < 0 || !HASH64.test(idempotencyKey) || !Array.isArray(factRefs) || factRefs.length === 0 || factRefs.length > MAX_AUDIT_REFS || !factRefs.every((ref) => FACT_REF.test(ref)) || !['inserted', 'duplicate', 'superseded'].includes(status)) throw errorFor('gbrain_codex_subscription_remember_receipt_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      if (candidateIndex >= GBRAIN_CODEX_SUBSCRIPTION_MAX_CANDIDATES) throw errorFor('gbrain_codex_subscription_remember_receipt_invalid')
      if (job.remembered.some((item) => item.idempotency_key === idempotencyKey)) return Object.freeze({ status: 'duplicate' })
      const intent = job.remember_intents.find((item) => item.idempotency_key === idempotencyKey)
      if (!intent) throw errorFor('gbrain_codex_subscription_remember_intent_missing')
      if (intent.candidate_index !== candidateIndex) throw errorFor('gbrain_codex_subscription_remember_receipt_invalid')
      const entry = { candidate_index: candidateIndex, idempotency_key: idempotencyKey, fact_refs: [...new Set(factRefs)], status }
      const updated = {
        ...job,
        remembered: [...job.remembered, entry],
        remember_intents: job.remember_intents.map((item) => item.idempotency_key === idempotencyKey ? { ...item, status: 'completed', fact_refs: entry.fact_refs } : item),
        fact_refs: [...new Set([...job.fact_refs, ...factRefs])].slice(0, MAX_AUDIT_REFS),
        updated_at: iso(now)
      }
      validateJob(updated)
      await atomicWriteJson(path, updated, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'recorded', fact_refs: entry.fact_refs })
    })
  }

  async function markRememberAmbiguous ({ jobId, ownerId, leaseToken, idempotencyKey, auditRefs = [] } = {}) {
    if (!HASH64.test(idempotencyKey) || !Array.isArray(auditRefs) || auditRefs.length > MAX_AUDIT_REFS || !auditRefs.every(validateAuditRef)) throw errorFor('gbrain_codex_subscription_remember_ambiguity_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      if (!job.remember_intents.some((item) => item.idempotency_key === idempotencyKey)) throw errorFor('gbrain_codex_subscription_remember_intent_missing')
      const currentMs = nowMilliseconds(now)
      const retried = {
        ...job,
        status: 'retry',
        lease: null,
        next_attempt_at: iso(currentMs),
        remember_intents: job.remember_intents.map((item) => item.idempotency_key === idempotencyKey ? { ...item, status: 'ambiguous', fact_refs: [] } : item),
        audit_refs: [...new Set([...job.audit_refs, ...auditRefs])].slice(0, MAX_AUDIT_REFS),
        last_error: 'remember_outcome_ambiguous',
        updated_at: iso(currentMs),
        completed_at: null
      }
      validateJob(retried)
      await atomicWriteJson(path, retried, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'retry', reason: 'remember_outcome_ambiguous', next_attempt_at: retried.next_attempt_at })
    })
  }

  async function complete ({ jobId, ownerId, leaseToken, facts = [], factRefs = [], auditRefs = [], curationRuntime } = {}) {
    if (!Array.isArray(factRefs) || factRefs.length > MAX_AUDIT_REFS || !factRefs.every((ref) => FACT_REF.test(ref))) throw errorFor('gbrain_codex_subscription_fact_refs_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      if (facts.some((fact) => !job.remembered.some((item) => item.idempotency_key === fact.idempotency_key))) throw errorFor('gbrain_codex_subscription_remember_receipt_required')
      const completed = terminalJob(job, 'completed', now, {
        facts,
        factRefs: [...job.fact_refs, ...factRefs, ...facts.flatMap((fact) => Array.isArray(fact.fact_refs) ? fact.fact_refs : [])],
        auditRefs: [...job.audit_refs, ...auditRefs],
        ...(curationRuntime !== undefined ? { curationRuntime } : {})
      })
      validateJob(completed)
      await atomicWriteJson(path, completed, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'completed', job_id: jobId, fact_refs: completed.fact_refs })
    })
  }

  async function fail ({ jobId, ownerId, leaseToken, reason = 'worker_failed', retryable = true, recoveryOnly = false, terminalRecovery = false, facts, auditRefs = [] } = {}) {
    if (!boundedText(reason, 128) || !SAFE_TOKEN.test(reason)) throw errorFor('gbrain_codex_subscription_failure_reason_invalid')
    if (!Array.isArray(auditRefs) || auditRefs.length > MAX_AUDIT_REFS || !auditRefs.every(validateAuditRef)) throw errorFor('gbrain_codex_subscription_audit_refs_invalid')
    if (typeof recoveryOnly !== 'boolean' || typeof terminalRecovery !== 'boolean') throw errorFor('gbrain_codex_subscription_recovery_flags_invalid')
    return withLock(async () => {
      const path = jobPath(jobId)
      const job = validateJob(await readJsonFile(fsImpl, path))
      if (job.status !== 'running' || job.lease?.owner_id !== ownerId || job.lease?.token !== leaseToken) return Object.freeze({ status: 'fenced' })
      const currentMs = nowMilliseconds(now)
      const recoveryAttempts = job.recovery_attempts
      if (terminalRecovery || (recoveryOnly && Number.isSafeInteger(recoveryAttempts) && recoveryAttempts >= 3)) {
        const terminalReason = terminalRecovery ? reason : (job.last_error || reason)
        const failed = terminalJob(job, 'failed', currentMs, { reason: terminalReason, facts: Array.isArray(facts) ? facts : job.facts, auditRefs: [...job.audit_refs, ...auditRefs] })
        validateJob(failed)
        await atomicWriteJson(path, failed, { replace: true, tokenFactory, fsImpl })
        return Object.freeze({ status: 'failed', reason: terminalReason })
      }
      const nativeRunStarted = job.curation_runtime?.status === 'started' || (job.curation_runtime && job.curation_runtime.status === undefined)
      if (recoveryOnly && nativeRunStarted) {
        const nextRecoveryAttempts = Number.isSafeInteger(recoveryAttempts) ? recoveryAttempts : 0
        const retried = {
          ...job,
          status: 'retry',
          recovery_attempts: nextRecoveryAttempts,
          facts: Array.isArray(facts) ? facts : job.facts,
          audit_refs: [...new Set([...job.audit_refs, ...auditRefs])].slice(0, MAX_AUDIT_REFS),
          lease: null,
          next_attempt_at: iso(currentMs + (reason === PAX_CURATION_TRANSPORT_ERROR
            ? retryDelay(reason, nextRecoveryAttempts + 1, backoffBaseMs, backoffMaxMs)
            : Math.floor(backoff(nextRecoveryAttempts + 1, backoffBaseMs, backoffMaxMs) * (0.5 + Math.random() / 2)))),
          last_error: reason,
          updated_at: iso(currentMs)
        }
        validateJob(retried)
        await atomicWriteJson(path, retried, { replace: true, tokenFactory, fsImpl })
        return Object.freeze({ status: 'retry', next_attempt_at: retried.next_attempt_at, recovery_attempts: nextRecoveryAttempts })
      }
      if (retryable && job.attempts < maxAttempts) {
        const retried = {
          ...job,
          status: 'retry',
          facts: Array.isArray(facts) ? facts : job.facts,
          audit_refs: [...new Set([...job.audit_refs, ...auditRefs])].slice(0, MAX_AUDIT_REFS),
          lease: null,
          next_attempt_at: iso(currentMs + retryDelay(reason, job.attempts, backoffBaseMs, backoffMaxMs)),
          last_error: reason,
          updated_at: iso(currentMs)
        }
        validateJob(retried)
        await atomicWriteJson(path, retried, { replace: true, tokenFactory, fsImpl })
        return Object.freeze({ status: 'retry', next_attempt_at: retried.next_attempt_at })
      }
      const failed = terminalJob(job, 'failed', currentMs, { reason, facts: Array.isArray(facts) ? facts : job.facts, auditRefs: [...job.audit_refs, ...auditRefs] })
      validateJob(failed)
      await atomicWriteJson(path, failed, { replace: true, tokenFactory, fsImpl })
      return Object.freeze({ status: 'failed' })
    })
  }

  async function listJobs () {
    return readAllJobs()
  }

  // Recovery metadata belongs to this queue; original jobs and inputs stay immutable.
  const recoveryDir = join(absoluteStateDir, 'recovery')
  async function withRecoveryLock (operation) {
    // Intake retention must not depend on the admission lock that just failed.
    const lock = await acquireLock({ path: join(recoveryDir, '.intake.lock'), directory: recoveryDir, prepare: async () => {
      await ensureDirectory(fsImpl, absoluteStateDir, { recursive: true })
      await ensureDirectory(fsImpl, recoveryDir)
    } })
    try { return await operation() } finally { await lock.release() }
  }
  async function withDispositionLock (operation) {
    // Always acquire admission before recovery; intake retention only needs recovery.
    return withLock(() => withRecoveryLock(operation))
  }
  async function readDisposition (id) {
    if (!HASH64.test(id)) throw errorFor('gbrain_codex_subscription_recovery_id_invalid')
    try { return await readJsonFile(fsImpl, join(recoveryDir, `${id}.json`)) } catch (error) {
      if (error.code === 'ENOENT') return null
      throw error
    }
  }
  async function recordIntakeFailure (input, reason) {
    // Validate metadata/source without rewriting the rejected original payload.
    validateInput({ ...input, candidates: [] })
    if (!boundedText(reason, 128) || !SAFE_TOKEN.test(reason)) throw errorFor('gbrain_codex_subscription_failure_reason_invalid')
    let id
    try { id = computeGbrainCodexSubscriptionInputHash(input) } catch {
      // A rejected binding has no valid request ID yet; retain its exact bytes and source.
      id = createHash('sha256').update(gbrainCodexSubscriptionCanonicalJson(input)).digest('hex')
    }
    return withRecoveryLock(async () => {
      const previous = await readDisposition(id)
      if (previous) return { recovery_id: id, status: previous.status }
      await atomicWriteJson(join(recoveryDir, `${id}.json`), { recovery_id: id, kind: 'intake_failed', status: 'open', input, reason, created_at: iso(now) }, { replace: false, tokenFactory, fsImpl })
      await auditLedger.record({ operation: 'codex_subscription_enqueue', phase: 'failed', idempotency_key: `${id}:intake:failed`, source_ref: input.source_ref, session_ref: `codex-session:${input.session_hash}`, turn_ref: `codex-turn:${input.turn_hash}`, error_code: reason })
      return { recovery_id: id, status: 'open' }
    })
  }
  async function pendingRecovery ({ after, limit = 20, actionable_only = false } = {}) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 20 || (after !== undefined && !HASH64.test(after))) throw errorFor('gbrain_codex_subscription_recovery_page_invalid')
    const jobs = await readAllJobs()
    const items = []
    for (const job of jobs.filter(job => job.status === 'failed')) {
      // A derived recovery job already owns the original failure.
      if (jobs.some(other => other.recovery_of?.job_id === job.job_id)) continue
      const state = await readDisposition(job.input_hash)
      items.push({ recovery_id: job.input_hash, kind: 'curation_failed', job_id: job.job_id, request_id: job.request_id, source_ref: job.source_ref, origin: job.origin, submitter: job.submitter, reason: job.last_error, actionable: state?.status !== 'blocked' && !(state?.handoff?.status === 'running' && dispatcherProcessAlive(state.handoff.dispatcher_process)), ...(state?.handoff ? { handoff: state.handoff } : {}), ...(state?.blocker ? { blocker: state.blocker } : {}) })
    }
    try {
      for (const name of await fsImpl.readdir(recoveryDir)) {
        if (!/^[a-f0-9]{64}\.json$/u.test(name)) continue
        const state = await readDisposition(name.slice(0, -5))
        if (state.kind !== 'intake_failed' || state.status === 'completed' || jobs.some(job => job.source_ref === state.input.source_ref)) continue
        items.push({ recovery_id: state.recovery_id, kind: state.kind, source_ref: state.input.source_ref, origin: state.input.origin, submitter: state.input.submitter, reason: state.reason, actionable: state.status !== 'blocked' && !(state.handoff?.status === 'running' && dispatcherProcessAlive(state.handoff.dispatcher_process)), ...(state.handoff ? { handoff: state.handoff } : {}), ...(state.blocker ? { blocker: state.blocker } : {}) })
      }
    } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (typeof auditLedger?.read === 'function') {
      const ledger = await auditLedger.read()
      for (const event of ledger.events.filter(event => event.operation === 'codex_subscription_enqueue' && event.phase === 'failed')) {
        const id = event.idempotency_key?.split(':')[0]
        if (!HASH64.test(id || '') || items.some(item => item.recovery_id === id) || jobs.some(job => job.source_ref === event.source_ref) || ledger.events.some(other => other.operation === event.operation && other.phase === 'completed' && other.source_ref === event.source_ref)) continue
        const state = await readDisposition(id)
        items.push({ recovery_id: id, kind: 'intake_source_only', source_ref: event.source_ref, reason: event.error_code, actionable: state?.status !== 'blocked', origin: null, submitter: null, ...(state?.blocker ? { blocker: state.blocker } : {}) })
      }
    }
    const selected = items.filter(item => (!after || item.recovery_id > after) && (!actionable_only || item.actionable)).sort((a, b) => a.recovery_id.localeCompare(b.recovery_id))
    return { tasks: selected.slice(0, limit), next_cursor: selected.length > limit ? selected[limit - 1].recovery_id : null }
  }
  async function blockRecovery ({ recovery_id: id, blocker }) {
    if (!isRecord(blocker) || !['needs_input', 'capability', 'source_unavailable'].includes(blocker.kind) || !boundedText(blocker.detail, 2000) || !boundedText(blocker.next_action, 2000) || !Array.isArray(blocker.evidence_refs) || !blocker.evidence_refs.length || blocker.evidence_refs.length > 16 || !blocker.evidence_refs.every(ref => boundedText(ref, 512))) throw errorFor('gbrain_codex_subscription_recovery_blocker_invalid')
    // An intake outage must not prevent recording its unresolved blocker.
    // Admission resolves this disposition under the same recovery lock.
    const retained = await readDisposition(id)
    const lockDisposition = retained?.kind === 'intake_failed' ? withRecoveryLock : withDispositionLock
    return lockDisposition(async () => {
      const old = await readDisposition(id)
      if (old?.handoff?.status === 'running' && dispatcherProcessAlive(old.handoff.dispatcher_process)) return { status: 'active', recovery_id: id, handoff: old.handoff }
      let job
      try { job = await readJob(`job-${id}`) } catch (error) {
        if (error.code !== 'ENOENT') throw error
        if (!old) {
          const ledger = await auditLedger.read()
          if (!ledger.events.some(event => event.operation === 'codex_subscription_enqueue' && event.phase === 'failed' && event.idempotency_key?.startsWith(`${id}:`))) throw errorFor('gbrain_codex_subscription_recovery_not_found')
        }
      }
      if (job && job.status !== 'failed') return { status: 'fenced' }
      await ensureDirectory(fsImpl, recoveryDir)
      const state = { ...(old || { recovery_id: id, kind: 'curation_failed' }), status: 'blocked', blocker, updated_at: iso(now) }
      await atomicWriteJson(join(recoveryDir, `${id}.json`), state, { tokenFactory, fsImpl })
      return { status: 'blocked', recovery_id: id, blocker }
    })
  }
  async function recover ({ recovery_id: id, callVerb }) {
    const state = await readDisposition(id)
    let job
    try { job = await readJob(`job-${id}`) } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (!job && state?.kind === 'intake_failed') {
      const result = await enqueue(state.input)
      if (['queued', 'duplicate'].includes(result.queue_status)) await withDispositionLock(async () => {
        const latest = await readDisposition(id)
        await atomicWriteJson(join(recoveryDir, `${id}.json`), { ...latest, status: 'admitted', result, ...(latest.blocker ? { blocker: { ...latest.blocker, resolved_at: iso(now) } } : {}), updated_at: iso(now) }, { tokenFactory, fsImpl })
      })
      return result
    }
    if (!job) return { status: 'blocked', recovery_id: id, reason: 'original_input_and_submitter_required' }
    if (job.status === 'completed') return { status: 'completed', job_id: job.job_id, fact_refs: job.fact_refs }
    if (job.status !== 'failed') return { status: job.status, queue_status: 'duplicate', job_id: job.job_id, request_id: job.request_id }
    const identity = { jobId: job.job_id, expectedRequestId: job.request_id, expectedSourceRef: job.source_ref, expectedInputHash: job.input_hash, expectedLastError: job.last_error }
    if (typeof callVerb !== 'function') return { status: 'blocked', reason: 'native_history_readback_required', recovery_id: id }
    const readback = await reconcileOriginalHistory(job, callVerb)
    if (readback.status !== 'history_missing') return readback
    // Attested native execution must be reconciled first, never replayed blindly.
    if (job.curation_runtime) {
      if (job.last_error === 'gbrain_shadow_timeout' && !job.recovery_of) return reconcileFailedRecoveryFromHistory({ ...identity, callVerb })
      if (job.recovery_of && ['pax_memory_curation_result_invalid', 'gbrain_codex_subscription_curation_receipt_unverified'].includes(job.last_error)) return reconcileFailedRecoveryFromHistory({ ...identity, expectedRecoveryOf: job.recovery_of, callVerb })
      return { status: 'blocked', reason: 'native_history_readback_required', recovery_id: id }
    }
    return enqueueRecoveryFromFailed(identity)
  }

  async function readProposalRecovery (id) {
    const state = await readDisposition(id)
    let job
    try { job = await readJob(`job-${id}`) } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (!job && state?.kind !== 'intake_failed') throw errorFor('gbrain_codex_subscription_original_input_required')
    const input = job || state.input
    const original = { input_hash: job?.input_hash || id, request_id: job?.request_id || null, source_ref: input.source_ref, origin: input.origin || null, submitter: input.submitter || null, status: job?.status || state.status, reason: job?.last_error || state.reason }
    if (!original.origin || !original.submitter) throw errorFor('gbrain_codex_subscription_original_author_required')
    const proposalInput = original.origin === 'agent' ? {
      candidates: structuredClone(input.candidates),
      ...(Object.hasOwn(input, 'original_turn') ? { original_turn: input.original_turn } : {}),
      source_ref: input.source_ref,
      session_hash: input.session_hash,
      turn_hash: input.turn_hash,
      origin: original.origin,
      submitter: original.submitter
    } : null
    const linkedJobs = job ? (await readAllJobs()).filter(other => other.recovery_of?.job_id === job.job_id) : []
    const linked = linkedJobs.find(other => other.status === 'completed')
    const active = linkedJobs.find(other => ['queued', 'retry', 'running'].includes(other.status))
    let handoff = state?.handoff || null
    if (handoff?.status === 'running' && !dispatcherProcessAlive(handoff.dispatcher_process)) handoff = { ...handoff, status: 'unresolved', error_code: 'proposal_handoff_dispatcher_stopped' }
    return { schema_version: 'oos.proposal_recovery.v1', recovery_id: id, original, ...(proposalInput ? { proposal_input: proposalInput } : {}), ...(job ? { original_job_ref: jobPath(job.job_id) } : {}), handoff, handoff_history: state?.handoff_history || [], blocker: state?.blocker || null, completed: job?.status === 'completed' || Boolean(linked), ...(linked ? { recovery_job_ref: jobPath(linked.job_id) } : {}), ...(active ? { active_recovery_job_ref: jobPath(active.job_id) } : {}), task_ref: join(recoveryDir, `${id}.json`) }
  }

  async function claimProposalHandoff ({ recovery_id: id, handoff_id: handoffId, target, previous_handoff_id: previousId, session_id: sessionId, evidence_refs: evidenceRefs, next_action: nextAction, dispatcher_process: dispatcher }) {
    if (!boundedText(handoffId, 128) || !SAFE_TOKEN.test(handoffId)) throw errorFor('gbrain_codex_subscription_handoff_id_invalid')
    if (target !== undefined && !['otto', 'pax'].includes(target)) throw errorFor('gbrain_codex_subscription_handoff_target_invalid')
    dispatcherProcessAlive(dispatcher)
    await ensureDirectory(fsImpl, recoveryDir)
    return withDispositionLock(async () => {
      const task = await readProposalRecovery(id)
      const state = await readDisposition(id) || { recovery_id: id, kind: 'curation_failed', status: 'open' }
      const previous = task.handoff
      const technicalOwner = target ?? previous?.target ?? 'otto'
      if (!['otto', 'pax'].includes(technicalOwner)) throw errorFor('gbrain_codex_subscription_handoff_target_invalid')
      if (task.completed || task.active_recovery_job_ref || task.original.status === 'running' || task.original.status === 'queued' || task.original.status === 'retry' || previous?.status === 'running') return { acquired: false, task }
      if (previous && previousId === undefined) return { acquired: false, task }
      let continuation
      if (previousId !== undefined) {
        if (!previous || previous.handoff_id !== previousId || previous.target !== technicalOwner || !sessionId || (previous.session_id && previous.session_id !== sessionId)) throw errorFor('gbrain_codex_subscription_handoff_continuation_conflict')
        if (!Array.isArray(evidenceRefs) || !evidenceRefs.length || evidenceRefs.length > 16 || !evidenceRefs.every(ref => boundedText(ref, 512)) || !boundedText(nextAction, 2000)) throw errorFor('gbrain_codex_subscription_handoff_evidence_required')
        continuation = { previous_handoff_id: previousId, evidence_refs: [...new Set(evidenceRefs)].sort(), next_action: nextAction }
        if ([...task.handoff_history, previous].some(item => item.handoff_id === handoffId || (item.continuation?.next_action === nextAction && gbrainCodexSubscriptionCanonicalJson(item.continuation.evidence_refs) === gbrainCodexSubscriptionCanonicalJson(continuation.evidence_refs)))) throw errorFor('gbrain_codex_subscription_handoff_unchanged')
      }
      const handoff = { handoff_id: handoffId, target: technicalOwner, status: 'running', dispatcher_process: dispatcher, started_at: iso(now), ...(sessionId ? { session_id: sessionId } : {}), ...(continuation ? { continuation } : {}) }
      const updated = { ...state, original: task.original, ...(task.original_job_ref ? { original_job_ref: task.original_job_ref } : {}), handoff, ...(previous ? { handoff_history: [...task.handoff_history, previous] } : {}) }
      await atomicWriteJson(task.task_ref, updated, { tokenFactory, fsImpl })
      return { acquired: true, task: { ...task, handoff } }
    })
  }

  async function finishProposalHandoff ({ recovery_id: id, handoff_id: handoffId, result, recovery }) {
    return withDispositionLock(async () => {
      const state = await readDisposition(id)
      if (state?.handoff?.handoff_id !== handoffId) throw errorFor('gbrain_codex_subscription_handoff_identity_conflict')
      if (state.handoff.status !== 'running') return { acquired: false, handoff: state.handoff }
      if (result?.target && result.target !== state.handoff.target) throw errorFor('gbrain_codex_subscription_handoff_identity_conflict')
      if (state.handoff.session_id && result?.session_id && state.handoff.session_id !== result.session_id) throw errorFor('gbrain_codex_subscription_handoff_identity_conflict')
      const task = await readProposalRecovery(id)
      const handoff = { ...state.handoff, status: task.completed ? 'completed' : 'unresolved', finished_at: iso(now), ...(result?.session_id ? { session_id: result.session_id } : {}), ...(result?.receipt_ref ? { receipt_ref: result.receipt_ref } : {}), recovery, ...(task.completed ? {} : { error_code: result?.timed_out ? 'proposal_owner_timeout' : 'proposal_outcome_unresolved' }) }
      await atomicWriteJson(task.task_ref, { ...state, handoff }, { tokenFactory, fsImpl })
      return { status: handoff.status, recovery_id: id, handoff, recovery }
    })
  }

  // Explicit internal owner operation. Never invoked by recover(), workers or tool registration.
  async function reconcileHistoricalReceipt (input) {
    const fence = reason => { throw errorFor(`gbrain_codex_subscription_historical_${reason}`) }
    if (!exactKeys(input, ['recovery_id', 'native_session_id']) || !HASH64.test(input.recovery_id) ||
        typeof input.native_session_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$/u.test(input.native_session_id)) fence('identity_invalid')
    if (!historicalReceipt || typeof historicalReceipt.callVerb !== 'function' || sourceId !== 'business-pb' ||
        typeof auditLedger?.record !== 'function' || typeof auditLedger?.read !== 'function') fence('bindings_missing')
    const id = input.recovery_id
    const failureDir = join(absoluteStateDir, 'historical-receipts', 'failures')
    const proofDir = join(absoluteStateDir, 'historical-receipts', 'proofs')
    const digestRaw = bytes => createHash('sha256').update(bytes).digest('hex')
    const resultFor = job => ({ status: 'completed', execution_kind: 'historical_receipt_reconciliation',
      job_id: job.job_id, request_id: job.request_id, fact_refs: job.fact_refs,
      historical_runtime: job.historical_reconciliation.historical_runtime })
    const eventFor = (job, proof) => ({ ...auditIdentity(job), operation: 'codex_subscription_curate',
      transport_verb: 'historical_receipt_reconciliation', phase: 'completed',
      idempotency_key: `${id}:historical-receipt:completed`, result_ref: `local-pax-historical-receipt:${id}`,
      result_fact_refs: proof.fact_refs, result_count: proof.fact_refs.length, error_code: null })
    async function verifyAudit (expected) {
      const ledger = await auditLedger.read()
      const events = ledger?.events?.filter(event => event.idempotency_key === expected.idempotency_key)
      if (ledger?.complete !== true || events?.length !== 1 ||
          Object.entries(expected).some(([key, value]) => canonicalJson(events[0][key]) !== canonicalJson(value))) fence('audit_unverified')
    }
    return withDispositionLock(async () => {
      const path = jobPath(`job-${id}`)
      const bytes = await readQueueBytes(fsImpl, path)
      const job = validateJob(JSON.parse(bytes.toString('utf8')))
      // Completion retries attest retained durable artifacts, never manufacture another execution/audit.
      if (job.status === 'completed') {
        const provenance = job.historical_reconciliation
        if (!provenance || provenance.native_session_id !== input.native_session_id) fence('completed_identity_conflict')
        const failureBytes = await readQueueBytes(fsImpl, join(failureDir, `${id}.json`), 256 * 1024)
        const failure = JSON.parse(failureBytes.toString('utf8'))
        const raw = Buffer.from(failure.raw_base64, 'base64')
        const original = validateJob(JSON.parse(raw.toString('utf8')))
        if (failure.schema !== 'pb.pax_historical_failure.v1' || failure.job_ref !== path || failure.source_ref !== original.source_ref ||
            failure.raw_sha256 !== digestRaw(raw) || failure.job_sha256 !== sha256(original) ||
            provenance.failure_raw_sha256 !== failure.raw_sha256 || provenance.job_sha256 !== failure.job_sha256) fence('failure_unverified')
        // Non-replacing publication verifies bytes, permissions and pinned-directory identity on retry too.
        await persistPaxHistoricalEvidence(failureDir, id, failure)
        const proofBytes = await readQueueBytes(fsImpl, join(proofDir, `${id}.json`), 256 * 1024)
        const proof = JSON.parse(proofBytes.toString('utf8'))
        if (digestRaw(proofBytes) !== provenance.receipt_sha256 || proof.job_sha256 !== sha256(original) ||
            proof.recovery_id !== id || proof.native?.session_id !== input.native_session_id) fence('receipt_unverified')
        await persistPaxHistoricalEvidence(proofDir, id, proof)
        const expected = { ...terminalJob(original, 'completed', job.completed_at, {
          factRefs: proof.fact_refs, auditRefs: [...original.audit_refs, provenance.failure_ref, provenance.receipt_ref] }),
        historical_reconciliation: provenance }
        if (canonicalJson(expected) !== canonicalJson(job)) fence('completed_job_changed')
        await verifyAudit(eventFor(original, proof))
        // A prior write may have renamed successfully then failed before directory sync.
        await syncDirectory(fsImpl, jobsDir)
        return resultFor(job)
      }
      if (job.status !== 'failed' || !hasNoLocalOutcomes(job) || job.lease !== null || job.audit_refs.length > MAX_AUDIT_REFS - 2) fence('job_ineligible')
      const state = await readDisposition(id)
      if ((await readAllJobs()).some(other => other.recovery_of?.job_id === job.job_id && ['queued', 'retry', 'running', 'completed'].includes(other.status)) ||
          [state?.handoff, ...(state?.handoff_history || [])].some(handoff => ['running', 'completed'].includes(handoff?.status)) ||
          ['admitted', 'completed'].includes(state?.status)) fence('linked_recovery_conflict')
      const reconciler = createPaxHistoricalReceiptReconciler({ ...historicalReceipt, readJob, auditDir: proofDir })
      // Validate eligibility and all independent evidence inside admission -> recovery locks.
      const verified = await reconciler.reconcile(input)
      const failure = { schema: 'pb.pax_historical_failure.v1', job_ref: path, source_ref: job.source_ref,
        raw_base64: bytes.toString('base64'), raw_sha256: digestRaw(bytes), job_sha256: sha256(job) }
      await persistPaxHistoricalEvidence(failureDir, id, failure)
      const provenance = { schema: 'pb.pax_historical_completion.v1', execution_kind: 'historical_receipt_reconciliation',
        historical_runtime: verified.receipt.historical_runtime, native_session_id: input.native_session_id,
        job_sha256: sha256(job), failure_raw_sha256: failure.raw_sha256,
        failure_ref: `local-pax-historical-failure:${id}`, receipt_ref: `local-pax-historical-receipt:${id}`,
        receipt_sha256: verified.receipt_sha256, audit_key: `${id}:historical-receipt:completed` }
      const event = eventFor(job, verified.receipt)
      await auditLedger.record(event)
      await verifyAudit(event)
      // A persisted proof is not authority: refresh native/backend/facts after the audit as well.
      const fresh = await reconciler.reconcile(input)
      if (fresh.receipt_sha256 !== verified.receipt_sha256) fence('evidence_changed')
      const completed = { ...terminalJob(job, 'completed', now, { factRefs: fresh.receipt.fact_refs,
        auditRefs: [...job.audit_refs, provenance.failure_ref, provenance.receipt_ref] }), historical_reconciliation: provenance }
      validateJob(completed)
      // Last asynchronous operation before the validated write checks the complete raw source again.
      if (!(await readQueueBytes(fsImpl, path)).equals(bytes)) fence('job_changed')
      await atomicWriteJson(path, completed, { tokenFactory, fsImpl })
      return resultFor(completed)
    })
  }

  async function reconcileOriginalHistory (observed, callVerb) {
    if (observed.status === 'completed') return { status: 'completed', job_id: observed.job_id, fact_refs: observed.fact_refs }
    if (observed.status !== 'failed') return { status: 'fenced' }
    const parent = await validateRecoveryParent(observed)
    if (parent === false) return { status: 'fenced', reason: 'recovery_parent_identity_unverified' }
    const requestId = observed.request_id || `pax-curation:${observed.input_hash}`
    const response = await callVerb('curation_history', { limit: 100, request_id: requestId })
    if (!isRecord(response) || !Array.isArray(response.items) || response.items.length > 100 || (response.next_cursor !== null && typeof response.next_cursor !== 'string')) throw errorFor('gbrain_codex_subscription_history_readback_invalid')
    const history = response.items.find(item => item.request_id === requestId)
    if (!history) return { status: 'history_missing' }
    if (!observed.curation_runtime || observed.curation_runtime.status === 'unknown' || history.pax_run !== requestId || !Array.isArray(history.decisions) || !history.decisions.length || history.decisions.length > 4) return { status: 'fenced', reason: 'native_history_identity_unverified' }
    const refs = []
    for (const decision of history.decisions) {
      if (!isRecord(decision) || !['add', 'merge', 'replace', 'retract', 'no_change'].includes(decision.action) || decision.visibility !== 'world' || !boundedText(decision.reason, 2000) || !Array.isArray(decision.source_refs) || !decision.source_refs.includes(observed.source_ref) || !decision.source_refs.every(ref => boundedText(ref, 512)) || !Array.isArray(decision.old_fact_ids) || !decision.old_fact_ids.every(id => Number.isSafeInteger(id) && id > 0) || (decision.new_fact_id !== null && (!Number.isSafeInteger(decision.new_fact_id) || decision.new_fact_id < 1))) return { status: 'fenced', reason: 'native_history_decision_unverified' }
      if ((decision.action === 'no_change' && (decision.old_fact_ids.length || decision.new_fact_id !== null)) || (decision.action === 'add' && (decision.old_fact_ids.length || decision.new_fact_id === null)) || (['merge', 'replace'].includes(decision.action) && (!decision.old_fact_ids.length || decision.new_fact_id === null)) || (decision.action === 'retract' && (!decision.old_fact_ids.length || decision.new_fact_id !== null))) return { status: 'fenced', reason: 'native_history_decision_unverified' }
      refs.push(...decision.old_fact_ids.map(id => `gbrain-fact:${id}`))
      if (decision.new_fact_id !== null) {
        const facts = await callVerb('list_facts', { after_id: decision.new_fact_id - 1, limit: 1 })
        const fact = facts?.facts?.find(fact => fact.id === decision.new_fact_id)
        if (!fact || fact.source_id !== sourceId || fact.visibility !== 'world' || fact.source_session !== requestId) return { status: 'fenced', reason: 'native_fact_readback_unverified' }
        refs.push(`gbrain-fact:${decision.new_fact_id}`)
      }
    }
    const auditRef = `local-pax-curation:${observed.input_hash}`
    return withLock(async () => {
      const current = await readJob(observed.job_id)
      if (gbrainCodexSubscriptionCanonicalJson(current) !== gbrainCodexSubscriptionCanonicalJson(observed)) return { status: 'fenced' }
      if (parent && gbrainCodexSubscriptionCanonicalJson(await validateRecoveryParent(current)) !== gbrainCodexSubscriptionCanonicalJson(parent)) return { status: 'fenced' }
      // Terminal queue records redact candidates; attest the stored original hash,
      // never recompute a different request from their redacted projection.
      await auditLedger.record({ ...auditIdentity(current), operation: 'codex_subscription_curate', transport_verb: 'codex_subscription_curate', phase: 'completed', idempotency_key: `${current.input_hash}:curate:${Math.max(1, current.attempts)}:completed`, result_ref: auditRef, result_fact_refs: [...new Set(refs)], result_count: history.decisions.length })
      const completed = terminalJob(current, 'completed', now, { factRefs: refs, auditRefs: [...current.audit_refs, auditRef] })
      validateJob(completed)
      await atomicWriteJson(jobPath(current.job_id), completed, { tokenFactory, fsImpl })
      return { status: 'completed', job_id: current.job_id, request_id: requestId, fact_refs: completed.fact_refs, replay: true }
    })
  }

  return Object.freeze({
    stateDir: absoluteStateDir,
    jobsDir,
    auditLedger,
    maxAttempts,
    leaseMs,
    enqueue,
    enqueueJob: enqueue,
    enqueueRecoveryFromFailed,
    reconcileFailedRecoveryFromHistory,
    claimNext,
    renewLease,
    leaseOwned,
    recordCurationRuntime,
    resetStartedCurationRuntime,
    prepareRemember,
    recordRemembered,
    markRememberAmbiguous,
    complete,
    fail,
    readJob,
    listJobs,
    recordIntakeFailure,
    pendingRecovery,
    blockRecovery,
    recover,
    reconcileHistoricalReceipt,
    readProposalRecovery,
    claimProposalHandoff,
    finishProposalHandoff,
    jobPath
  })
}

export const __TEST__ = Object.freeze({
  inputBinding,
  validateInput,
  validateJob,
  atomicWriteJson
})
