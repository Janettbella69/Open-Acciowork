export type DeletionDomain = { id: string; label: string; files: number; bytes: number }
export type DeletionRetained = { code: string; message: string; count: number }
export type ProjectDeletionPreview = {
  schema_version: 1
  project_id: string
  project_name: string
  fingerprint: string
  domains: DeletionDomain[]
  counts: { files: number; bytes: number; runs: number; sessions: number; memories: number; shops: number }
  retained: DeletionRetained[]
  blockers: { code: string; message: string }[]
  can_delete: boolean
}
export type ProjectDeletionRequest = {
  operation_id: string
  fingerprint: string
  confirmation_name: string
  reason: string
}
export type ProjectDeletionOperation = {
  schema_version: 1
  project_id: string
  operation_id: string
  status: 'deleting' | 'blocked' | 'deleted'
  created_at: string
  updated_at: string
  domains: DeletionDomain[]
  retained: DeletionRetained[]
  error_code: string | null
  error_message: string | null
  can_retry: boolean
}
export type ConversationDeletionPreview = ProjectDeletionPreview & { run_id: string; run_ids: string[] }
export type ConversationDeletionOperation = ProjectDeletionOperation & { run_id: string; run_ids: string[] }
export type DeletionHandle = { projectId: string; operationId: string; runId?: string }

export function requireDeletionPreview(projectId: string, value: ProjectDeletionPreview) {
  if (value.schema_version !== 1 || value.project_id !== projectId || !value.project_name
    || !/^[a-f0-9]{64}$/.test(value.fingerprint)) throw new Error('The deletion review did not match this project.')
}

export function requireDeletionOperation(handle: DeletionHandle, value: ProjectDeletionOperation) {
  if (value.schema_version !== 1 || value.project_id !== handle.projectId || value.operation_id !== handle.operationId
    || !['deleting', 'blocked', 'deleted'].includes(value.status)) throw new Error('The deletion response did not match this operation.')
  if (handle.runId) requireConversationScope(handle.runId, value)
}

export function requireConversationScope(runId: string, value: unknown): asserts value is { run_id: string; run_ids: string[] } {
  const row = value as Partial<ConversationDeletionOperation>
  if (row?.run_id !== runId || !Array.isArray(row.run_ids) || !row.run_ids.includes(runId)
    || row.run_ids.some((id) => typeof id !== 'string' || !/^run_[A-Za-z0-9_-]+$/.test(id))
    || new Set(row.run_ids).size !== row.run_ids.length) throw new Error('The deletion response did not match this conversation.')
}
