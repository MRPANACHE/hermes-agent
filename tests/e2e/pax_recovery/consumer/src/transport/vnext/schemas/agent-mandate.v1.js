import { createHash } from 'node:crypto'

export const SCHEMA_ID = 'oos.agent_mandate.v1'
export const ACTION_KINDS = Object.freeze([
  'code_change',
  'config_change',
  'deploy',
  'data_mutation',
  'bid_or_price_mutation',
  'policy_change',
  'external_side_effect',
  'read_only_observation'
])
export const ISSUERS = Object.freeze(['david', 'mara', 'control_room'])

const MANDATE_FIELDS = Object.freeze([
  'schema_id', 'schema_version', 'signed', 'signature_key_id', 'signature',
  'mandate_id', 'mandate_ref', 'mandate_hash', 'objective_scope_hash', 'version',
  'agent_run_id', 'oos_agent_id', 'project_id', 'repo', 'start_sha', 'objective',
  'success_criteria', 'evidence_contract', 'scope_in', 'scope_out', 'target_refs',
  'allowed_action_kinds', 'allowed_tools', 'allowed_capabilities', 'issued_at',
  'expires_at', 'issuer', 'standing_policy_refs', 'production_authority',
  'data_authority', 'economic_authority', 'external_effect_authority',
  'rollback_contract', 'stop_contract', 'escalation_contract', 'freedom_envelope',
  'requires_reapproval'
])

const INPUT_FIELDS = Object.freeze(MANDATE_FIELDS.filter((field) => ![
  'schema_id', 'schema_version', 'signed', 'signature_key_id', 'signature',
  'mandate_hash', 'objective_scope_hash'
].includes(field)))

const TEXT_MAX = 4_096
const SHA256_RE = /^[a-f0-9]{64}$/
const GIT_SHA_RE = /^[a-f0-9]{40}$/

export const schema = Object.freeze({
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  additionalProperties: false,
  required: [...MANDATE_FIELDS],
  properties: {
    schema_id: { const: SCHEMA_ID },
    schema_version: { const: SCHEMA_ID },
    signed: { const: true },
    signature_key_id: { type: 'string', minLength: 1 },
    signature: { type: 'string', minLength: 1 },
    mandate_id: { type: 'string', minLength: 1 },
    mandate_ref: { type: 'string', minLength: 1 },
    mandate_hash: { type: 'string', pattern: '^[a-f0-9]{64}$' },
    objective_scope_hash: { type: 'string', pattern: '^[a-f0-9]{64}$' },
    version: { type: 'integer', minimum: 1 },
    agent_run_id: { type: 'string', minLength: 1 },
    oos_agent_id: { type: 'string', minLength: 1 },
    project_id: { type: 'string', minLength: 1 },
    repo: { type: 'string', minLength: 1 },
    start_sha: { type: 'string', pattern: '^[a-f0-9]{40}$' },
    objective: { type: 'string', minLength: 1 },
    success_criteria: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    evidence_contract: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    scope_in: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    scope_out: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    target_refs: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    allowed_action_kinds: { type: 'array', minItems: 1, items: { enum: ACTION_KINDS } },
    allowed_tools: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    allowed_capabilities: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
    issued_at: { type: 'string', format: 'date-time' },
    expires_at: { type: 'string', format: 'date-time' },
    issuer: { enum: ISSUERS },
    standing_policy_refs: { type: 'array', items: { type: 'string', minLength: 1 } },
    production_authority: { type: 'boolean' },
    data_authority: { type: 'boolean' },
    economic_authority: { type: 'boolean' },
    external_effect_authority: { type: 'boolean' },
    rollback_contract: { type: 'object', additionalProperties: false },
    stop_contract: { type: 'object', additionalProperties: false },
    escalation_contract: { type: 'object', additionalProperties: false },
    freedom_envelope: { type: 'object', additionalProperties: false },
    requires_reapproval: { type: 'array', items: { type: 'string', minLength: 1 } }
  }
})

export class MandateValidationError extends Error {
  constructor (message, path = '$') {
    super(`${path}: ${message}`)
    this.name = 'MandateValidationError'
    this.code = 'MANDATE_SCHEMA_INVALID'
    this.path = path
  }
}

function fail (path, message) {
  throw new MandateValidationError(message, path)
}

function isPlainRecord (value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype !== null && Object.getPrototypeOf(prototype) === null && Object.prototype.toString.call(prototype) === '[object Object]'
}

function inspectPlainJson (value, path, seen) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || Object.is(value, -0)) fail(path, 'finite non-negative-zero number required')
    return
  }
  if (typeof value !== 'object') fail(path, 'plain JSON value required')
  if (seen.has(value)) fail(path, 'cyclic value is not canonical')
  seen.add(value)
  if (Object.getOwnPropertySymbols(value).length > 0) fail(path, 'symbol keys are not canonical')
  if (Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value)
    if (prototype === null || Object.prototype.toString.call(prototype) !== '[object Array]' || typeof prototype.constructor !== 'function' || prototype.constructor.name !== 'Array') fail(path, 'plain array required')
    for (const name of Object.getOwnPropertyNames(value)) {
      if (name === 'length') continue
      if (!/^\d+$/.test(name) || Number(name) >= value.length) fail(`${path}.${name}`, 'array property is not canonical')
      const descriptor = Object.getOwnPropertyDescriptor(value, name)
      if (!descriptor || descriptor.get || descriptor.set || !descriptor.enumerable) fail(`${path}.${name}`, 'accessor or non-enumerable property is not canonical')
      inspectPlainJson(value[name], `${path}[${name}]`, seen)
    }
    for (let index = 0; index < value.length; index += 1) {
      if (!Object.hasOwn(value, index)) fail(`${path}[${index}]`, 'sparse arrays are not canonical')
    }
  } else {
    if (!isPlainRecord(value)) fail(path, 'plain object required')
    for (const name of Object.getOwnPropertyNames(value)) {
      const descriptor = Object.getOwnPropertyDescriptor(value, name)
      if (!descriptor || descriptor.get || descriptor.set || !descriptor.enumerable) fail(`${path}.${name}`, 'accessor or non-enumerable property is not canonical')
      inspectPlainJson(value[name], `${path}.${name}`, seen)
    }
  }
  seen.delete(value)
}

export function assertPlainJson (value, path = '$') {
  inspectPlainJson(value, path, new WeakSet())
  return value
}

function exactKeys (value, allowed, path) {
  const allowedSet = new Set(allowed)
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) fail(`${path}.${key}`, 'unknown field')
  }
}

function text (value, path, max = TEXT_MAX) {
  if (typeof value !== 'string' || value.trim() === '' || value.includes('\u0000')) fail(path, 'non-empty text required')
  if (value.length > max) fail(path, `text length must be <= ${max}`)
  return value
}

function stringArray (value, path, { min = 0, max = 256 } = {}) {
  if (!Array.isArray(value) || value.length < min || value.length > max) fail(path, `array length must be between ${min} and ${max}`)
  const seen = new Set()
  value.forEach((item, index) => {
    text(item, `${path}[${index}]`, 2_048)
    if (seen.has(item)) fail(`${path}[${index}]`, 'duplicate item')
    seen.add(item)
  })
  return value
}

function timestamp (value, path) {
  text(value, path, 32)
  if (!Number.isFinite(Date.parse(value)) || new Date(value).toISOString() !== value) fail(path, 'canonical ISO timestamp required')
  return value
}

function hash (value, path) {
  text(value, path, 64)
  if (!SHA256_RE.test(value)) fail(path, 'lowercase SHA-256 required')
  return value
}

function gitSha (value, path) {
  text(value, path, 40)
  if (!GIT_SHA_RE.test(value)) fail(path, 'full lowercase git SHA required')
  return value
}

function boolean (value, path) {
  if (typeof value !== 'boolean') fail(path, 'boolean required')
  return value
}

function contract (value, path, fields) {
  if (!isPlainRecord(value)) fail(path, 'plain object required')
  exactKeys(value, fields, path)
  return value
}

function validateContracts (mandate) {
  const rollback = contract(mandate.rollback_contract, 'rollback_contract', ['required', 'rollback_ref', 'stop_ref', 'conditions'])
  boolean(rollback.required, 'rollback_contract.required')
  if (rollback.rollback_ref !== null) text(rollback.rollback_ref, 'rollback_contract.rollback_ref', 1_024)
  if (rollback.required && rollback.rollback_ref === null) fail('rollback_contract.rollback_ref', 'required when rollback is required')
  text(rollback.stop_ref, 'rollback_contract.stop_ref', 1_024)
  stringArray(rollback.conditions, 'rollback_contract.conditions', { min: 1, max: 64 })

  const stop = contract(mandate.stop_contract, 'stop_contract', ['conditions', 'action'])
  stringArray(stop.conditions, 'stop_contract.conditions', { min: 1, max: 64 })
  text(stop.action, 'stop_contract.action', 2_048)

  const escalation = contract(mandate.escalation_contract, 'escalation_contract', ['conditions', 'route', 'david_visible'])
  stringArray(escalation.conditions, 'escalation_contract.conditions', { min: 1, max: 64 })
  text(escalation.route, 'escalation_contract.route', 1_024)
  boolean(escalation.david_visible, 'escalation_contract.david_visible')

  const freedom = contract(mandate.freedom_envelope, 'freedom_envelope', [
    'may_replan', 'may_choose_hypotheses', 'may_choose_task_order', 'may_run_targeted_tests', 'allowlisted_tools'
  ])
  for (const field of ['may_replan', 'may_choose_hypotheses', 'may_choose_task_order', 'may_run_targeted_tests']) {
    if (freedom[field] !== true) fail(`freedom_envelope.${field}`, 'must be true inside the signed freedom envelope')
  }
  stringArray(freedom.allowlisted_tools, 'freedom_envelope.allowlisted_tools', { min: 1, max: 128 })
}

export function canonicalize (value, path = '$') {
  assertPlainJson(value, path)
  if (value === null || typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number') return value
  if (Array.isArray(value)) return value.map((item, index) => canonicalize(item, `${path}[${index}]`))
  const result = {}
  for (const key of Object.keys(value).sort()) {
    Object.defineProperty(result, key, {
      value: canonicalize(value[key], `${path}.${key}`),
      enumerable: true,
      writable: true,
      configurable: true
    })
  }
  return result
}

export function canonicalJson (value) {
  return JSON.stringify(canonicalize(value))
}

export function canonicalBytes (value) {
  return Buffer.from(canonicalJson(value), 'utf8')
}

export function sha256Bytes (bytes) {
  return createHash('sha256').update(bytes).digest('hex')
}

export function canonicalSha256 (value) {
  return sha256Bytes(canonicalBytes(value))
}

export function objectiveScopePayload (mandate) {
  return {
    objective: mandate.objective,
    success_criteria: mandate.success_criteria,
    evidence_contract: mandate.evidence_contract,
    scope_in: mandate.scope_in,
    scope_out: mandate.scope_out,
    target_refs: mandate.target_refs,
    allowed_action_kinds: mandate.allowed_action_kinds,
    stop_contract: mandate.stop_contract,
    rollback_contract: mandate.rollback_contract
  }
}

export function objectiveScopeHash (mandate) {
  return canonicalSha256(objectiveScopePayload(mandate))
}

export function hashMandateArtifact (mandate) {
  assertPlainJson(mandate)
  const { mandate_hash: _mandateHash, ...payload } = mandate
  return canonicalSha256(payload)
}

export function signingPayload (mandate) {
  assertPlainJson(mandate)
  const {
    mandate_hash: _mandateHash,
    signature: _signature,
    signature_key_id: _signatureKeyId,
    signed: _signed,
    ...payload
  } = mandate
  return {
    ...payload,
    signed: false,
    signature_key_id: null,
    signature: null,
    mandate_hash: null
  }
}

function validateInputFields (value) {
  if (!isPlainRecord(value)) fail('$', 'plain object required')
  exactKeys(value, INPUT_FIELDS, '$')
  for (const field of ['mandate_id', 'mandate_ref', 'agent_run_id', 'oos_agent_id', 'project_id', 'repo', 'objective']) text(value[field], field)
  if (!Number.isSafeInteger(value.version) || value.version < 1) fail('version', 'positive safe integer required')
  gitSha(value.start_sha, 'start_sha')
  stringArray(value.success_criteria, 'success_criteria', { min: 1, max: 128 })
  stringArray(value.evidence_contract, 'evidence_contract', { min: 1, max: 128 })
  stringArray(value.scope_in, 'scope_in', { min: 1, max: 256 })
  stringArray(value.scope_out, 'scope_out', { min: 1, max: 256 })
  stringArray(value.target_refs, 'target_refs', { min: 1, max: 256 })
  stringArray(value.allowed_action_kinds, 'allowed_action_kinds', { min: 1, max: ACTION_KINDS.length })
  value.allowed_action_kinds.forEach((item, index) => {
    if (!ACTION_KINDS.includes(item)) fail(`allowed_action_kinds[${index}]`, 'unknown action kind')
  })
  stringArray(value.allowed_tools, 'allowed_tools', { min: 1, max: 128 })
  stringArray(value.allowed_capabilities, 'allowed_capabilities', { min: 1, max: 128 })
  timestamp(value.issued_at, 'issued_at')
  timestamp(value.expires_at, 'expires_at')
  if (Date.parse(value.expires_at) <= Date.parse(value.issued_at)) fail('expires_at', 'must be after issued_at')
  if (!ISSUERS.includes(value.issuer)) fail('issuer', 'unknown issuer')
  stringArray(value.standing_policy_refs, 'standing_policy_refs', { max: 128 })
  for (const field of ['production_authority', 'data_authority', 'economic_authority', 'external_effect_authority']) boolean(value[field], field)
  validateContracts(value)
  stringArray(value.requires_reapproval, 'requires_reapproval', { max: 32 })
  return value
}

export function validateMandate (value) {
  assertPlainJson(value)
  if (!isPlainRecord(value)) fail('$', 'plain object required')
  exactKeys(value, MANDATE_FIELDS, '$')
  if (value.schema_id !== SCHEMA_ID || value.schema_version !== SCHEMA_ID) fail('schema_id', `must equal ${SCHEMA_ID}`)
  if (value.signed !== true) fail('signed', 'signed mandate required')
  text(value.signature_key_id, 'signature_key_id', 512)
  text(value.signature, 'signature', 4_096)
  validateInputFields(Object.fromEntries(INPUT_FIELDS.map((field) => [field, value[field]])))
  hash(value.mandate_hash, 'mandate_hash')
  hash(value.objective_scope_hash, 'objective_scope_hash')
  if (value.objective_scope_hash !== objectiveScopeHash(value)) fail('objective_scope_hash', 'does not match objective/scope envelope')
  if (value.mandate_hash !== hashMandateArtifact(value)) fail('mandate_hash', 'does not match canonical signed artifact')
  return value
}

export const MANDATE_FIELD_NAMES = MANDATE_FIELDS
export const MANDATE_INPUT_FIELD_NAMES = INPUT_FIELDS
