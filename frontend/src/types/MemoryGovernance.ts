import type { MemoryItem } from '../api/types'

export type GovernedMemory = MemoryItem & {
  expires_at?: string | null
  expired?: boolean
}

export type MemoryExpiryUpdate = { ttl_seconds: number } | { clear_expiry: true }

export type MemoryTombstone = {
  schema_version: 1
  memory_id: string
  scope: MemoryItem['scope']
  scope_id: string
  last_version: number
  last_status: MemoryItem['status']
  content_sha256: string
  deleted_by: string
  reason: string
  deleted_at: string
}

export type MemoryExport = {
  schema_version: 'memory-export-v1'
  exported_at: string
  exported_by: string
  count: number
  items: GovernedMemory[]
  tombstone_count: number
  tombstones: MemoryTombstone[]
}
