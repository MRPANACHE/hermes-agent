import { createHash } from 'node:crypto';

export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
}

export function sha256(value) {
  return createHash('sha256').update(typeof value === 'string' ? value : canonicalJson(value)).digest('hex');
}

export function hashWithoutField(value, field) {
  const copy = { ...value };
  delete copy[field];
  // The event proof stays outside the PB content hash; including it would
  // create a record-hash -> event-hash -> record-hash dependency.
  delete copy.attestation;
  return sha256(copy);
}
