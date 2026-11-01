import { type ProfileRouteOptions, resolveProfileBackendRoute } from './connection-config'
import type { GatewayEndpoint } from './local-gateway'

export interface OwnerConnection {
  mode?: string
  gatewayEndpoint?: GatewayEndpoint
  profile?: string
  sharedPrimary?: boolean
}

/** Sharing a machine does not imply sharing its native session authority. Discover the
 * requested profile through the existing cached pool before reusing a local primary. */
export async function resolveLocalProfileOwner<T extends OwnerConnection>({
  connection,
  profile,
  primaryProfile,
  attachProfile
}: {
  connection: T
  profile: string
  primaryProfile: string
  attachProfile: () => Promise<T>
}): Promise<T> {
  const owner = connection.gatewayEndpoint

  if (connection.mode !== 'local' || !owner || profile === primaryProfile) {
    return connection
  }

  const attached = await attachProfile()
  const target = attached.gatewayEndpoint

  if (attached.mode === 'local' && target && owner.instance_id === target.instance_id
      && owner.authority_epoch === target.authority_epoch && owner.api_origin === target.api_origin) {
    return connection
  }

  // The existing profile pool retains this descriptor and its lifecycle. A sibling owner's
  // ticket must never be minted against the primary owner's control home.
  return { ...attached, sharedPrimary: false }
}


export interface ProfileBackendRequest {
  method?: string
  path?: string
}

export interface LocalProfilePoolEntry<T> {
  connectionPromise: Promise<T> | null
  separateNativeOwner?: boolean
}

export interface LocalProfileRoutingDependencies<T extends OwnerConnection> {
  primaryProfile: () => string
  routeOptions: (profile: string, request?: ProfileBackendRequest) => ProfileRouteOptions
  assertCanStart: (profile: string) => void
  startPrimary: (profile: string) => Promise<T>
  pool: Map<string, LocalProfilePoolEntry<T>>
  newEntry: () => LocalProfilePoolEntry<T>
  dialProfile: (profile: string, entry: LocalProfilePoolEntry<T>) => Promise<T>
  assertNotPassiveSpawn: (passive: boolean, profile: string) => void
  recordLocal: (profile: string, local: boolean) => void
  forgetFailedEntry: (profile: string, entry: LocalProfilePoolEntry<T>) => void
  logFailure: (scope: string, error: unknown) => void
  log: (message: string) => void
  invalidatePrimary: () => void
}

/** Own the existing local descriptor cache policy; process startup and retirement remain
 * with Electron's existing pool callbacks, not with this routing module. */
export function createLocalProfileBackendRouting<T extends OwnerConnection>(deps: LocalProfileRoutingDependencies<T>) {
  const keyFor = (profile?: null | string) => profile && String(profile).trim() ? String(profile).trim() : deps.primaryProfile()

  async function pooled(key: string, passive: boolean): Promise<T> {
    const existing = deps.pool.get(key)

    if (existing) {
      const connection = await existing.connectionPromise!
      deps.recordLocal(key, connection.mode !== 'remote')

      return connection
    }

    deps.assertNotPassiveSpawn(passive, key)
    const entry = deps.newEntry()
    entry.connectionPromise = deps.dialProfile(key, entry).catch(error => {
      deps.logFailure(`"${key}"`, error)
      deps.forgetFailedEntry(key, entry)
      throw error
    })
    deps.pool.set(key, entry)
    const connection = await entry.connectionPromise
    deps.recordLocal(key, connection.mode !== 'remote')

    return connection
  }

  async function ensure(profile?: null | string, opts: { passive?: boolean; request?: ProfileBackendRequest } = {}): Promise<T> {
    const key = keyFor(profile)
    const passive = Boolean(opts.passive)
    deps.assertCanStart(key)
    const route = resolveProfileBackendRoute(key, deps.routeOptions(key, opts.request))

    if (route.backend !== 'primary') {
      return pooled(key, passive)
    }

    const connection = await deps.startPrimary(key)
    deps.recordLocal(key, connection.mode !== 'remote')

    const ownerConnection = await resolveLocalProfileOwner({
      connection,
      profile: key,
      primaryProfile: deps.primaryProfile(),
      attachProfile: () => pooled(key, passive)
    })

    const entry = deps.pool.get(key)

    if (entry) {
      entry.separateNativeOwner = ownerConnection !== connection
    }

    if (ownerConnection !== connection) {
      return ownerConnection
    }

    return route.descriptorProfile
      ? { ...connection, profile: route.descriptorProfile, sharedPrimary: true }
      : { ...connection, profile: key }
  }

  async function forget(profile?: null | string, request?: ProfileBackendRequest): Promise<void> {
    const key = keyFor(profile)
    const options = deps.routeOptions(key, request)
    const route = resolveProfileBackendRoute(key, options)
    const entry = deps.pool.get(key)

    if (route.backend === 'primary' && entry?.separateNativeOwner
        && !options.globalRemote && !options.primaryRemoteActive && !options.profileRemoteOverride) {
      deps.pool.delete(key)
      deps.log(`[gateway] forgot stale separate native owner for profile "${key}"; re-ensuring`)

      return
    }

    if (route.backend === 'primary') {
      deps.pool.delete(key)
      deps.invalidatePrimary()
      deps.log(`[gateway] forgot stale canonical endpoint for primary "${key}"; re-ensuring`)

      return
    }

    if (deps.pool.delete(key)) {
      deps.log(`[gateway] forgot stale canonical endpoint for profile "${key}"; re-ensuring`)
    }
  }

  return { ensure, forget }
}
