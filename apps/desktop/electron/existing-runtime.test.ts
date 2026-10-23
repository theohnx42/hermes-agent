import { describe, expect, it, vi } from 'vitest'

import { requireExistingRuntime } from './existing-runtime'

describe('runtime use outside explicit setup', () => {
  it('never invokes installation for ticket, secondary-profile, or restart requests', async () => {
    for (const operation of ['gateway ticket', 'secondary profile', 'gateway restart']) {
      const install = vi.fn(async (runtime: { bootstrap: boolean }) => ({ operation, runtime }))
      const request = async () => install(requireExistingRuntime({ bootstrap: true }))

      await expect(request()).rejects.toThrow('this request will not install Hermes')
      expect(install).not.toHaveBeenCalled()
    }
  })

  it('preserves the validated runtime and the callers lifecycle check', async () => {
    const backend = { bootstrap: false, command: 'existing-launcher', profile: 'condor' }
    const prepare = vi.fn(async (runtime: typeof backend) => runtime)

    await expect(prepare(requireExistingRuntime(backend))).resolves.toBe(backend)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(prepare).toHaveBeenCalledWith(backend)

    const cancelled = vi.fn((runtime: typeof backend) => {
      expect(runtime).toBe(backend)
      throw new Error('owner cancelled')
    })
    expect(() => cancelled(requireExistingRuntime(backend))).toThrow('owner cancelled')
  })
})
