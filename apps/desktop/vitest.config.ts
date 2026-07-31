import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

const resourceConstrained = process.env.HERMES_RESOURCE_CONSTRAINED_TESTS === '1'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform.
    // Keep normal development strict, while allowing the immutable release
    // controller to identify an explicitly resource-constrained staging host.
    testTimeout: resourceConstrained ? 60_000 : 15_000,
    maxWorkers: resourceConstrained ? 1 : undefined
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
