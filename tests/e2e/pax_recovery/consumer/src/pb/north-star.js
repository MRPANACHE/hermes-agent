import { canonicalJson, sha256 } from '../episode-ledger/hash.js';

const record = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const text = (value, max = 500) => typeof value === 'string' && value.length > 0 && Array.from(value).length <= max && value.trim() === value;
const ref = value => text(value) && /^[a-z][a-z0-9-]*:[\w./:@-]+$/u.test(value) && !value.includes('..');
const hash = value => typeof value === 'string' && /^sha256:[a-f0-9]{64}$/u.test(value);

/** Validate the shared OOS snapshot; a valid digest proves bytes, never approval authority. */
export function validateNorthStar(value, connection = null) {
  if (!record(value, ['schema_version', 'goal_ref', 'source_version', 'owner_ref', 'objective', 'metrics', 'digest']) ||
      value.schema_version !== 'oos.north_star.v1' || !ref(value.goal_ref) || !text(value.source_version) ||
      !ref(value.owner_ref) || !text(value.objective, 8000) || !hash(value.digest) ||
      !Array.isArray(value.metrics) || value.metrics.length > 100 ||
      Array.from(JSON.stringify(value)).length > 4000) throw new TypeError('pb_north_star_invalid');
  const keys = new Set();
  for (const metric of value.metrics) {
    if (!record(metric, ['key', 'definition_ref', 'definition_digest']) || !text(metric.key) || keys.has(metric.key) ||
        !ref(metric.definition_ref) || !hash(metric.definition_digest)) throw new TypeError('pb_north_star_invalid');
    keys.add(metric.key);
  }
  const { digest, ...body } = value;
  if (`sha256:${sha256(canonicalJson(body))}` !== digest) throw new TypeError('pb_north_star_digest_invalid');
  if (connection && (value.goal_ref !== connection.goal_ref || !Array.isArray(connection.metrics) ||
      !value.metrics.every(metric => connection.metrics.includes(metric.key)))) throw new TypeError('pb_north_star_connection_mismatch');
  return structuredClone(value);
}
