import { describe, expect, it, vi } from 'vitest'

import { resolveProfileBackendRoute } from './connection-config'
import { createLocalProfileBackendRouting, type ProfileBackendRequest, resolveLocalProfileOwner } from './local-gateway-owner-routing'

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

function harness(primaryProfile: string, primary: ReturnType<typeof connection>, target: ReturnType<typeof connection>) {
  const backendPool = new Map()
  const dial = vi.fn(async () => target)
  const startPrimary = vi.fn(async () => primary)

  const routeOptions = vi.fn((_profile: string, request?: ProfileBackendRequest) => ({
    primaryProfile,
    requestMethod: request?.method,
    requestPath: request?.path
  }))

  const state = { clearPromiseForAttempt: vi.fn() }

  const routing = createLocalProfileBackendRouting({
    primaryProfile: () => primaryProfile,
    routeOptions,
    assertCanStart: vi.fn(),
    startPrimary,
    pool: backendPool,
    newEntry: () => ({ connectionPromise: null }),
    dialProfile: dial,
    assertNotPassiveSpawn: (passive: boolean) => { if (passive) { throw new Error('passive spawn forbidden') } },
    recordLocal: vi.fn(),
    forgetFailedEntry: vi.fn(),
    logFailure: vi.fn(),
    log: vi.fn(),
    invalidatePrimary: state.clearPromiseForAttempt
  })

  return { ensure: routing.ensure, forget: routing.forget, dial, backendPool, state, startPrimary, routeOptions }
}

describe('native profile owner routing', () => {
  it('keeps an unscopable REST mutation on its profile pool when a failed request redials', async () => {
    const primary = connection('default', 'multiplexer', 31001)
    const target = connection('work', 'multiplexer', 31001)
    const { ensure, forget, dial, backendPool, state, startPrimary, routeOptions } = harness('default', primary, target)
    const request = { method: 'POST', path: '/api/not-a-real-route/destroy' }

    // Even the same authority cannot scope this write through the shared primary descriptor.
    expect(await ensure('work', { request })).toBe(target)
    expect(startPrimary).not.toHaveBeenCalled()

    // The REST failure seam forgets and re-ensures with the original request, not a read route.
    await forget('work', request)
    expect(backendPool.has('work')).toBe(false)
    expect(state.clearPromiseForAttempt).not.toHaveBeenCalled()
    const replacement = connection('work', 'replacement', 31002, 2)
    dial.mockResolvedValueOnce(replacement)
    expect(await ensure('work', { request })).toBe(replacement)
    expect(startPrimary).not.toHaveBeenCalled()
    expect(dial).toHaveBeenCalledTimes(2)
    expect(routeOptions.mock.calls).toEqual([
      ['work', request], ['work', request], ['work', request]
    ])
  })

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

  it('keeps passive discovery fail-closed until an existing pool descriptor is cached', async () => {
    const primary = connection('condor', 'standalone', 31001)
    const target = connection('codex', 'multiplexer', 31002)
    const { ensure, dial } = harness('condor', primary, target)
    await expect(ensure('codex', { passive: true })).rejects.toThrow('passive spawn forbidden')
    expect(dial).not.toHaveBeenCalled()
    await ensure('codex')
    expect(await ensure('codex', { passive: true })).toMatchObject({ baseUrl: target.baseUrl })
    expect(dial).toHaveBeenCalledTimes(1)
  })

  it('keeps remote primary routing on its existing connection without a local dial', async () => {
    const remote = { ...connection('condor', 'remote', 31001), mode: 'remote' }
    const { ensure, dial } = harness('condor', remote, connection('codex', 'multiplexer', 31002))
    expect(await ensure('codex')).toMatchObject({ mode: 'remote', baseUrl: remote.baseUrl })
    expect(dial).not.toHaveBeenCalled()
  })

  it('propagates discovery failure rather than minting against the wrong owner', async () => {
    const primary = connection('condor', 'standalone', 31001)

    await expect(resolveLocalProfileOwner({ connection: primary, profile: 'codex', primaryProfile: 'condor', attachProfile: async () => { throw new Error('profile unavailable') } })).rejects.toThrow('profile unavailable')
  })
})
