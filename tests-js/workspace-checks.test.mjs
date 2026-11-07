import { expect, test } from 'vitest'
import { partitionUnits } from '../.github/scripts/run-workspace-checks.mjs'

const units = [
  { pkg: 'apps/desktop', script: 'check:test:ui' },
  { pkg: 'apps/desktop', script: 'check:lint' },
  { pkg: 'tests-js', script: 'check' },
  { pkg: 'new-workspace', script: 'check:new' },
]

test('partitions preserve every discovered check and cover each native UI shard exactly once', () => {
  const partitions = Array.from({ length: 4 }, (_, i) => partitionUnits(units, `${i + 1}/4`))
  const all = partitions.flat()
  expect(all.filter(unit => !unit.args)).toEqual(units.slice(1))
  expect(all.filter(unit => unit.args).map(unit => unit.args).sort()).toEqual(
    Array.from({ length: 4 }, (_, i) => ['--', '--', `--shard=${i + 1}/4`, '--maxWorkers=2']),
  )
  expect(new Set(all.map(unit => JSON.stringify(unit))).size).toBe(all.length)
  expect(partitions.every(partition => partition.length > 0)).toBe(true)
})

test('unpartitioned execution retains the original complete check population', () => {
  expect(partitionUnits(units, null)).toBe(units)
})

test('malformed partitions refuse rather than silently dropping checks', () => {
  for (const partition of ['0/4', '5/4', '1/0', '1', '1/4garbage', 'invalid']) {
    expect(() => partitionUnits(units, partition)).toThrow('Invalid check partition')
  }
})
