import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Kleine CI-runners tonen meer host-CPU's dan hun eigen quota.
    maxWorkers: process.env.GITHUB_ACTIONS ? 2 : undefined,
    environment: 'node',
    include: ['**/*.test.ts', '**/*.test.mjs'],
  },
})
