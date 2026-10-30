// Not an executable entry point. Release/live admission is deliberately separate.
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { createPaxNativeReadChannel, recoveryId, nativeSession } from './channel.mjs'
const release = '/home/david/.local/share/panache-brain/releases/3d2ce28882926b7cef2066bc0d04ce7768605496'
const state = '/home/david/.local/state/operator-os/gbrain-codex-chat'
const queueDir = `${state}/subscription-extract`
const rawHash = '3d4780c2cafd4e2b37fadd7f107c05aa0f1371dd10fdfd9d392565129641dd7c'
const canonicalHash = '2555eaaa62c9cde50c5496f6320c188b7676aead040cff3e68a1f0dfd12d4689'
let invoked = false
const digest = bytes => createHash('sha256').update(bytes).digest('hex')

// Trusted host-only binding: no parameter selects endpoint, identity, tool, job,
// history, facts, filesystem path or consumer implementation.
export async function reconcileFixedPaxRecovery (inheritedSocket) {
  if (invoked) throw new Error('pax_native_retry_forbidden')
  invoked = true
  const channel = createPaxNativeReadChannel(inheritedSocket)
  try {
    if (process.execPath !== '/usr/bin/node') throw new Error('pax_native_node_mismatch')
    const seal = await readFile(`${release}/.pb-release-seal.json`)
    if (digest(seal) !== '9fe72068081804dd871c472e0733020773125dced2e9cf1b416b7bbe8f582765') throw new Error('pax_native_consumer_mismatch')
    // Verify the pinned sealed JS tree before dynamic imports; a receipt alone
    // is not evidence that the fixed deployment's actual source is unchanged.
    for (const [mode, hash, path] of JSON.parse(seal).files) {
      if (!path.startsWith('src/') || !path.endsWith('.js')) continue
      if (!['100644', '100755'].includes(mode) || path.split('/').includes('..')) throw new Error('pax_native_consumer_path')
      const bytes = await readFile(`${release}/${path}`)
      const actual = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex')
      if (actual !== hash) throw new Error('pax_native_consumer_source_changed')
    }
    const { sha256 } = await import(`${release}/src/episode-ledger/hash.js`)
    const bytes = await readFile(`${queueDir}/jobs/job-${recoveryId}.json`)
    if (digest(bytes) !== rawHash || sha256(JSON.parse(bytes)) !== canonicalHash) throw new Error('pax_native_job_changed')
    const { createGbrainBridgeAuditLedger } = await import(`${release}/src/transport/gbrain-bridge-audit.js`)
    const { createGbrainCodexSubscriptionQueue } = await import(`${release}/src/transport/gbrain-codex-subscription-queue.js`)
    const auditLedger = createGbrainBridgeAuditLedger({ stateDir: state })
    const ledger = await auditLedger.read()
    if (ledger.complete !== true || ledger.events.some(e => e.idempotency_key === `${recoveryId}:historical-receipt:completed`)) throw new Error('pax_native_audit_changed')
    const queue = createGbrainCodexSubscriptionQueue({ sourceId:'business-pb', stateDir:queueDir, auditLedger,
      historicalReceipt:{callVerb:channel.callVerb, profileRoot:'/home/david/.hermes/profiles'} })
    return await queue.reconcileHistoricalReceipt({recovery_id:recoveryId, native_session_id:nativeSession})
  } finally { channel.close() }
}
