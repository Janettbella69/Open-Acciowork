export type PermissionLifecycle = { durable?: boolean; state?: string; expiresAt?: string | null }

export function permissionLifecycleState(item: PermissionLifecycle): string {
  if (!item.durable) return 'pending'
  if (!item.expiresAt || !Number.isFinite(Date.parse(item.expiresAt))) return 'unknown'
  if (Date.parse(item.expiresAt) <= Date.now()) return 'expired'
  return item.state ?? 'unknown'
}

export function permissionConflict(cause: unknown): boolean {
  return cause !== null && typeof cause === 'object' && 'status' in cause && cause.status === 409
}
