import fs from 'node:fs'

import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

import { resolveProfileBackendRoute } from './connection-config'
import { resolveLocalProfileOwner } from './local-gateway-owner-routing'

function connection(profile: string, owner: string, port: number, epoch = 1) {
  const home = profile === 'default' ? '/private/hermes' : `/private/hermes/profiles/${profile}`

  return {
    mode: 'local',
    profile,
    baseUrl: `http://127.0.0.1:${port}`,
    gatewayEndpoint: {
      profile_id: home,
      instance_id: owner,
      authority_epoch: epoch,
      runtime_protocol: 1,
      api_origin: `http://127.0.0.1:${port}`,
      capabilities: ['session-authority-v1'],
      supervisor: 'test',
      control_home: owner === 'multiplexer' ? '/private/hermes' : null
    }
  }
}

// Execute the actual Electron orchestration bodies with owned process boundaries stubbed.
// Native discovery is the existing pool's dial, never a second gateway-run implementation.
function harness(primaryProfile: string, primary: ReturnType<typeof connection>, target: ReturnType<typeof connection>) {
  const text = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const file = ts.createSourceFile('main.ts', text, ts.ScriptTarget.Latest, true)

  const bodies = file.statements.filter((node): node is ts.FunctionDeclaration =>
    ts.isFunctionDeclaration(node) && ['ensureBackend', 'ensurePooledBackend', 'forgetLocalGatewayDescriptor'].includes(node.name?.text || '')
  ).map(node => node.getText(file)).join('\n')

  const backendPool = new Map()
  const dial = vi.fn(async () => target)

  const dependencies = {
    primaryProfileKey: () => primaryProfile,
    profileDeletionGate: { assertCanStart: vi.fn() },
    profileRouteOptions: () => ({ primaryProfile }),
    resolveProfileBackendRoute,
    startHermes: vi.fn(async () => primary),
    setWslBridgeProfileState: vi.fn(),
    resolveLocalProfileOwner,
    backendPool,
    rememberLog: vi.fn(),
    backendConnectionState: { startAttempt: vi.fn(() => ({})), getPromise: vi.fn(), clearPromiseForAttempt: vi.fn() },
    assertNotPassiveSpawn: (passive: boolean) => { if (passive) { throw new Error('passive spawn forbidden') } },
    newPoolEntry: () => ({}),
    dialPoolBackend: dial,
    logPoolSpawnFailure: vi.fn(),
    forgetFailedPoolEntry: vi.fn()
  }

  const javascript = ts.transpileModule(bodies, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const functions = new Function(...Object.keys(dependencies), `${javascript};return {ensureBackend,forgetLocalGatewayDescriptor}`)(...Object.values(dependencies))

  return { ensure: functions.ensureBackend, forget: functions.forgetLocalGatewayDescriptor, dial, backendPool, state: dependencies.backendConnectionState }
}

describe('native profile owner routing', () => {
  it.each([
    ['condor', 'codex', 'standalone', 'multiplexer'],
    ['default', 'condor', 'multiplexer', 'standalone']
  ])('attaches %s → %s to its existing distinct owner', async (primaryProfile, requested, owner, otherOwner) => {
    const primary = connection(primaryProfile, owner, 31001)
    const target = connection(requested, otherOwner, 31002)
    const { ensure, dial } = harness(primaryProfile, primary, target)
    const first = await ensure(requested)
    const second = await ensure(requested)

    expect(first).toMatchObject({ baseUrl: target.baseUrl, profile: requested, sharedPrimary: false })
    expect(first.gatewayEndpoint).toEqual(target.gatewayEndpoint)
    expect(second).toEqual(first)
    expect(dial).toHaveBeenCalledTimes(1)
    expect(primary.gatewayEndpoint.profile_id).not.toBe(target.gatewayEndpoint.profile_id)
  })

  it('reuses the primary socket only after discovering the same authority', async () => {
    const primary = connection('default', 'multiplexer', 31001)
    const target = connection('codex', 'multiplexer', 31001)
    const { ensure, dial } = harness('default', primary, target)

    expect(await ensure('codex')).toMatchObject({ baseUrl: primary.baseUrl, sharedPrimary: true, profile: 'codex' })
    expect(dial).toHaveBeenCalledTimes(1)
  })

  it('forgets a separate owner without invalidating the healthy primary, then redials', async () => {
    const primary = connection('condor', 'standalone', 31001)
    const target = connection('codex', 'multiplexer', 31002)
    const { ensure, forget, dial, state } = harness('condor', primary, target)
    await ensure('codex')
    await forget('codex')
    expect(state.clearPromiseForAttempt).not.toHaveBeenCalled()
    const replacement = connection('codex', 'replacement', 31003)
    dial.mockResolvedValueOnce(replacement)
    expect(await ensure('codex')).toMatchObject({ baseUrl: replacement.baseUrl, sharedPrimary: false })
    expect(dial).toHaveBeenCalledTimes(2)
  })

  it('forgets same-owner pooled discovery and the stale primary together', async () => {
    const primary = connection('default', 'multiplexer', 31001)
    const { ensure, forget, backendPool, state } = harness('default', primary, connection('codex', 'multiplexer', 31001))
    await ensure('codex')
    await forget('codex')
    expect(backendPool.has('codex')).toBe(false)
    expect(state.clearPromiseForAttempt).toHaveBeenCalledTimes(1)
  })

  it('does not rediscover its own primary profile', async () => {
    const primary = connection('condor', 'standalone', 31001)
    const { ensure, dial } = harness('condor', primary, primary)

    expect((await ensure('condor')).gatewayEndpoint).toEqual(primary.gatewayEndpoint)
    expect(dial).not.toHaveBeenCalled()
  })

  it('preserves remote descriptors without local discovery', async () => {
    const attachProfile = vi.fn()
    const remote = { mode: 'remote', baseUrl: 'https://remote.example' }

    expect(await resolveLocalProfileOwner({ connection: remote, profile: 'codex', primaryProfile: 'condor', attachProfile })).toBe(remote)
    expect(attachProfile).not.toHaveBeenCalled()
    expect(resolveProfileBackendRoute('codex', { primaryProfile: 'condor', profileRemoteOverride: true }).backend).toBe('pool')
    expect(resolveProfileBackendRoute('codex', { primaryProfile: 'condor', isolatedBackend: true }).backend).toBe('pool')
  })

  it('propagates discovery failure rather than minting against the wrong owner', async () => {
    const primary = connection('condor', 'standalone', 31001)

    await expect(resolveLocalProfileOwner({ connection: primary, profile: 'codex', primaryProfile: 'condor', attachProfile: async () => { throw new Error('profile unavailable') } })).rejects.toThrow('profile unavailable')
  })
})
