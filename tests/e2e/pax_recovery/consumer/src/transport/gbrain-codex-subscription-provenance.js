import {
  canonicalJson as canonicalJsonValue,
  canonicalSha256 as canonicalSha256Value,
  sha256Bytes
} from './vnext/schemas/agent-mandate.v1.js'

export const GBRAIN_CODEX_SUBSCRIPTION_MODEL = 'gpt-5.6-luna'

const SOURCE_REF = /^codex-conversations\/[a-f0-9]{32}\/[a-f0-9]{32}$/u
const HASH64 = /^[a-f0-9]{64}$/u
const CSX_PROVENANCE = /^src=(?<source_ref>codex-conversations\/[a-f0-9]{32}\/[a-f0-9]{32});mode=csx;model=gpt-5\.6-luna;in=(?<input_hash>[a-f0-9]{64});key=(?<idempotency_key>[a-f0-9]{64})$/u
const HASH32 = /^[a-f0-9]{32}$/u
const JOB_ID = /^job-[a-f0-9]{64}$/u
const SAFE_RECOVERY_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/u
const FACT_KINDS = Object.freeze(['preference', 'commitment', 'belief', 'fact'])
const MAX_CANDIDATES = 4
const MAX_CANDIDATE_LENGTH = 800
const MAX_ORIGINAL_TURN_LENGTH = 12_000
const MAX_PRIOR_CONTEXT_TURNS = 3
const MAX_ENTITY_LENGTH = 128
const SAFE_SUBMITTER = /^[A-Za-z0-9][A-Za-z0-9._:@+=,-]{0,255}$/u
const INPUT_KEYS = Object.freeze(['candidates', 'source_ref', 'session_hash', 'turn_hash'])
const INPUT_KEYS_WITH_ORIGINAL_TURN = Object.freeze(['candidates', 'original_turn', 'source_ref', 'session_hash', 'turn_hash'])
const PRIOR_CONTEXT_KEYS = Object.freeze(['content_hash', 'original_turn', 'source_ref'])
const IDEMPOTENCY_REQUIRED_KEYS = Object.freeze([
  'candidates', 'candidate_index', 'content_hash', 'entity', 'fact', 'input_hash',
  'model', 'session_hash', 'source_ref', 'turn_hash'
])
const IDEMPOTENCY_OPTIONAL_CLASSIFIER_KEYS = Object.freeze(['confidence', 'kind', 'original_turn', 'prior_context'])

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
  const wanted = [...expected].sort()
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index])
}

function exactKeysWithOptionalPriorContext (value, expected) {
  if (!isRecord(value)) return false
  return exactKeys(value, expected) || exactKeys(value, [...expected, 'prior_context'])
}

function keysAreRequiredWithOptional (value, required, optional) {
  if (!isRecord(value)) return false
  const allowed = new Set([...required, ...optional])
  const actual = Object.keys(value)
  return required.every((key) => actual.includes(key)) && actual.every((key) => allowed.has(key))
}

function canonicalSha256 (value) {
  try {
    return canonicalSha256Value(value)
  } catch {
    throw errorFor('gbrain_codex_subscription_canonical_json_invalid')
  }
}

function textSha256 (value) {
  return sha256Bytes(Buffer.from(value, 'utf8'))
}

function boundedText (value, max) {
  return typeof value === 'string' && value.length > 0 && value.length <= max && !value.includes('\0')
}

export function validateGbrainCodexSubscriptionPriorContext (value, sessionHash, sourceRef) {
  if (value === undefined) return Object.freeze([])
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_PRIOR_CONTEXT_TURNS) throw errorFor('gbrain_codex_subscription_prior_context_invalid')
  const seen = new Set()
  const context = []
  for (const item of value) {
    if (!exactKeys(item, PRIOR_CONTEXT_KEYS) || !SOURCE_REF.test(item.source_ref) || !HASH64.test(item.content_hash) || !boundedText(item.original_turn, MAX_ORIGINAL_TURN_LENGTH)) throw errorFor('gbrain_codex_subscription_prior_context_invalid')
    const [, itemSessionHash] = item.source_ref.split('/')
    if (itemSessionHash !== sessionHash || item.source_ref === sourceRef || seen.has(item.source_ref) || textSha256(item.original_turn) !== item.content_hash) throw errorFor('gbrain_codex_subscription_prior_context_invalid')
    seen.add(item.source_ref)
    context.push(Object.freeze({ source_ref: item.source_ref, content_hash: item.content_hash, original_turn: item.original_turn }))
  }
  return Object.freeze(context)
}

function validateBindingInput (input) {
  if (!isRecord(input)) throw errorFor('gbrain_codex_subscription_binding_invalid')
  const allowed = new Set([...INPUT_KEYS, 'original_turn', 'prior_context', 'origin', 'submitter'])
  const actual = Object.keys(input)
  if (!INPUT_KEYS.every(key => actual.includes(key)) || actual.some(key => !allowed.has(key))) throw errorFor('gbrain_codex_subscription_binding_invalid')
  const hasOriginalTurn = Object.hasOwn(input, 'original_turn')
  if (!SOURCE_REF.test(input.source_ref) || !HASH32.test(input.session_hash) || !HASH32.test(input.turn_hash) || !Array.isArray(input.candidates) || input.candidates.length > MAX_CANDIDATES || (hasOriginalTurn ? !boundedText(input.original_turn, MAX_ORIGINAL_TURN_LENGTH) : input.candidates.length === 0) || !input.candidates.every((candidate) => boundedText(candidate, MAX_CANDIDATE_LENGTH)) || (input.origin !== undefined && !['human', 'agent'].includes(input.origin)) || (input.submitter !== undefined && (!boundedText(input.submitter, 256) || !SAFE_SUBMITTER.test(input.submitter)))) throw errorFor('gbrain_codex_subscription_binding_invalid')
  const priorContext = validateGbrainCodexSubscriptionPriorContext(input.prior_context, input.session_hash, input.source_ref)
  return Object.freeze({
    ...(hasOriginalTurn ? { original_turn: input.original_turn } : {}),
    ...(priorContext.length > 0 ? { prior_context: priorContext } : {}),
    source_ref: input.source_ref,
    session_hash: input.session_hash,
    turn_hash: input.turn_hash,
    candidates: Object.freeze([...input.candidates]),
    ...(input.origin !== undefined ? { origin: input.origin } : {}),
    ...(input.submitter !== undefined ? { submitter: input.submitter } : {})
  })
}

export function gbrainCodexSubscriptionCanonicalJson (value) {
  try {
    return canonicalJsonValue(value)
  } catch {
    throw errorFor('gbrain_codex_subscription_canonical_json_invalid')
  }
}

export function gbrainCodexSubscriptionCanonicalSha256 (value) {
  return canonicalSha256(value)
}

export function gbrainCodexSubscriptionFactContentHash (factText) {
  if (!boundedText(factText, MAX_CANDIDATE_LENGTH)) throw errorFor('gbrain_codex_subscription_fact_text_invalid')
  return textSha256(factText)
}

export function gbrainCodexSubscriptionInputHash (input) {
  const binding = validateBindingInput(input)
  return canonicalSha256({
    ...(Object.hasOwn(binding, 'original_turn') ? { original_turn: binding.original_turn } : {}),
    ...(Object.hasOwn(binding, 'prior_context') ? { prior_context: binding.prior_context } : {}),
    source_ref: binding.source_ref,
    session_hash: binding.session_hash,
    turn_hash: binding.turn_hash,
    model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
    candidates: binding.candidates,
    ...(binding.origin !== undefined ? { origin: binding.origin } : {}),
    ...(binding.submitter !== undefined ? { submitter: binding.submitter } : {})
  })
}

export function gbrainCodexSubscriptionRecoveryInputHash (input) {
  if (!isRecord(input) || !Object.hasOwn(input, 'recovery_of')) throw errorFor('gbrain_codex_subscription_recovery_binding_invalid')
  const { recovery_of: recoveryOf, ...baseInput } = input
  const keys = ['job_id', 'input_hash', 'request_id', 'source_ref', 'last_error']
  if (!exactKeys(recoveryOf, keys) || !JOB_ID.test(recoveryOf.job_id) || !HASH64.test(recoveryOf.input_hash) || recoveryOf.job_id !== `job-${recoveryOf.input_hash}` || !boundedText(recoveryOf.request_id, 256) || !SAFE_RECOVERY_TOKEN.test(recoveryOf.request_id) || !SOURCE_REF.test(recoveryOf.source_ref) || recoveryOf.source_ref !== baseInput.source_ref || !boundedText(recoveryOf.last_error, 128) || !SAFE_RECOVERY_TOKEN.test(recoveryOf.last_error)) throw errorFor('gbrain_codex_subscription_recovery_binding_invalid')
  return canonicalSha256({
    schema: 'oos.gbrain_codex_subscription_recovery_input.v1',
    input_hash: gbrainCodexSubscriptionInputHash(baseInput),
    recovery_of: recoveryOf
  })
}

export function gbrainCodexSubscriptionIdempotencyKey (value) {
  if (!keysAreRequiredWithOptional(value, IDEMPOTENCY_REQUIRED_KEYS, IDEMPOTENCY_OPTIONAL_CLASSIFIER_KEYS)) throw errorFor('gbrain_codex_subscription_idempotency_binding_invalid')
  const input = validateBindingInput({
    candidates: value.candidates,
    ...(Object.hasOwn(value, 'original_turn') ? { original_turn: value.original_turn } : {}),
    ...(Object.hasOwn(value, 'prior_context') ? { prior_context: value.prior_context } : {}),
    ...(Object.hasOwn(value, 'origin') ? { origin: value.origin } : {}),
    ...(Object.hasOwn(value, 'submitter') ? { submitter: value.submitter } : {}),
    source_ref: value.source_ref,
    session_hash: value.session_hash,
    turn_hash: value.turn_hash
  })
  const expectedInputHash = gbrainCodexSubscriptionInputHash(input)
  if (!Number.isSafeInteger(value.candidate_index) || value.candidate_index < 0 || value.candidate_index >= MAX_CANDIDATES || !boundedText(value.fact, MAX_CANDIDATE_LENGTH) || (Object.hasOwn(value, 'kind') && !FACT_KINDS.includes(value.kind)) || (Object.hasOwn(value, 'confidence') && (typeof value.confidence !== 'number' || !Number.isFinite(value.confidence) || value.confidence < 0 || value.confidence > 1)) || !boundedText(value.entity, MAX_ENTITY_LENGTH) || !HASH64.test(value.input_hash) || value.input_hash !== expectedInputHash || value.model !== GBRAIN_CODEX_SUBSCRIPTION_MODEL || !HASH64.test(value.content_hash)) throw errorFor('gbrain_codex_subscription_idempotency_binding_invalid')
  const expectedContentHash = gbrainCodexSubscriptionFactContentHash(value.fact)
  if (value.content_hash !== expectedContentHash) throw errorFor('gbrain_codex_subscription_content_hash_mismatch')
  return canonicalSha256({
    source_ref: input.source_ref,
    session_hash: input.session_hash,
    turn_hash: input.turn_hash,
    model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
    input_hash: expectedInputHash,
    candidates: input.candidates,
    candidate_index: value.candidate_index,
    fact: value.fact,
    entity: value.entity,
    content_hash: value.content_hash
  })
}

export function buildGbrainCodexSubscriptionProvenance (value) {
  if (!exactKeys(value, ['source_ref', 'input_hash', 'idempotency_key']) || !SOURCE_REF.test(value.source_ref) || !HASH64.test(value.input_hash) || !HASH64.test(value.idempotency_key)) throw errorFor('gbrain_codex_subscription_provenance_invalid')
  const { source_ref, input_hash, idempotency_key } = value
  const provenance = `src=${source_ref};mode=csx;model=${GBRAIN_CODEX_SUBSCRIPTION_MODEL};in=${input_hash};key=${idempotency_key}`
  if (provenance.length > 256) throw errorFor('gbrain_codex_subscription_provenance_too_long')
  return provenance
}

export function parseGbrainCodexSubscriptionProvenance (value) {
  if (typeof value !== 'string') return null
  const match = CSX_PROVENANCE.exec(value)
  if (!match) return null
  const parsed = Object.freeze({
    source_ref: match.groups.source_ref,
    mode: 'csx',
    model: GBRAIN_CODEX_SUBSCRIPTION_MODEL,
    input_hash: match.groups.input_hash,
    idempotency_key: match.groups.idempotency_key
  })
  return buildGbrainCodexSubscriptionProvenance({
    source_ref: parsed.source_ref,
    input_hash: parsed.input_hash,
    idempotency_key: parsed.idempotency_key
  }) === value ? parsed : null
}
