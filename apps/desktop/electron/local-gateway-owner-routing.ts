import type { GatewayEndpoint } from './local-gateway'

type OwnerConnection = {
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
