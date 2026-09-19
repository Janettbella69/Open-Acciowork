import type { ProjectDeletionOperation, ProjectDeletionPreview, ProjectDeletionRequest } from '../api/projectDeletion'

export const deletionOperationId = '12345678-1234-4234-8234-123456789abc'
export function deletionPreview(projectId = 'project_1'): ProjectDeletionPreview {
  return { schema_version: 1, project_id: projectId, project_name: 'Supplier research', fingerprint: 'a'.repeat(64),
    domains: [{ id: 'workspace', label: 'Workspace files', files: 12, bytes: 2400 }],
    counts: { files: 12, bytes: 2400, runs: 3, sessions: 2, memories: 1, shops: 1 },
    retained: [{ code: 'external', message: 'Already downloaded copies remain outside the application.', count: 2 }],
    blockers: [], can_delete: true }
}
export const deletionRequest: ProjectDeletionRequest = {
  operation_id: deletionOperationId, fingerprint: 'a'.repeat(64), confirmation_name: 'Supplier research', reason: 'No longer needed',
}
export function deletionOperation(status: ProjectDeletionOperation['status'] = 'deleting'): ProjectDeletionOperation {
  return { schema_version: 1, project_id: 'project_1', operation_id: deletionOperationId, status,
    created_at: '2026-09-19T10:00:00Z', updated_at: '2026-09-19T10:01:00Z', domains: deletionPreview().domains,
    retained: deletionPreview().retained, error_code: status === 'blocked' ? 'SDK_BUSY' : null,
    error_message: status === 'blocked' ? 'The SDK process has not stopped.' : null, can_retry: status === 'blocked' }
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail })
  return { promise, resolve, reject }
}
