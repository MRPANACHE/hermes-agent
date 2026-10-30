import { createInterface } from 'node:readline'

export const recoveryId = '5e5c7051b1058e0f9b03d07afac9ec0da593adb8aee4db0135432c4dddcaa192'
export const nativeSession = '20261027_214817_10339b'
export const requestId = `pax-curation:${recoveryId}`
const fail = reason => { throw new Error(`pax_native_channel_${reason}`) }
const exact = (a, b) => JSON.stringify(Object.keys(a).sort()) === JSON.stringify(b.sort())

// Socket inherited directly from the native host, no listener/address/path or model API.
export function createPaxNativeReadChannel (socket) {
  const lines = createInterface({ input: socket, crlfDelay: Infinity })
  const iterator = lines[Symbol.asyncIterator]()
  let history, stopped = false, busy = false, rounds = 0
  const pending = new Set()
  async function exchange (message) {
    if (stopped || busy) fail('stopped_or_concurrent')
    busy = true
    let timer
    try {
      socket.write(`${JSON.stringify(message)}\n`)
      const frame = await Promise.race([
        iterator.next(),
        new Promise((resolve, reject) => { timer = setTimeout(() => reject(new Error('pax_native_channel_timeout')), 12000) })
      ])
      if (frame.done || Buffer.byteLength(frame.value) > 263168) fail('frame_invalid')
      const result = JSON.parse(frame.value)
      if (result.ok !== true || !exact(result, ['ok', 'value'])) fail('host_stopped')
      return result.value
    } catch (error) {
      stopped = true
      socket.destroy()
      throw error
    } finally { clearTimeout(timer); busy = false }
  }
  async function callVerb (verb, args) {
    try {
      if (verb === 'curation_history') {
        if (!args || !exact(args, ['request_id', 'limit']) || args.request_id !== requestId || args.limit !== 100 || pending.size || rounds >= 2) fail('request_mismatch')
        const value = await exchange({ op: 'history' })
        if (value?.next_cursor !== null || !Array.isArray(value.items) || value.items.length !== 1 ||
            value.items[0].request_id !== requestId || value.items[0].source_id !== 'business-pb') fail('history_invalid')
        history = value.items[0]
        if (!Array.isArray(history.decisions) || !history.decisions.length || history.decisions.length > 4) fail('history_invalid')
        for (let index = 0; index < history.decisions.length; index++) pending.add(index)
        rounds++
        return value
      }
      if (verb !== 'list_facts' || !args || !exact(args, ['after_id', 'limit']) || args.limit !== 1) fail('verb_or_arguments_invalid')
      const index = history?.decisions.findIndex(d => d.new_fact_id - 1 === args.after_id)
      if (!Number.isSafeInteger(index) || !pending.has(index)) fail('fact_request_invalid')
      const value = await exchange({ op: 'fact', index })
      pending.delete(index)
      return value
    } catch (error) {
      stopped = true
      socket.destroy()
      throw error
    }
  }
  return Object.freeze({ callVerb, close: () => { stopped = true; lines.close(); socket.destroy() } })
}
