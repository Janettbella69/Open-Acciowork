export type MaintenanceKind = 'backup' | 'restore'
export type MaintenancePrepare = { request_id: string; kind: MaintenanceKind; backup_id?: string | null }
export type MaintenanceOperation = MaintenancePrepare & {
  state: 'preparing' | 'reviewed' | 'publishing' | 'completed' | 'discarded'
  created_at: string; reviewed_at: string | null; fingerprint: string | null; destination: string
  summary: { files?: number; bytes?: number; projects?: number; runs?: number; proposals?: number; proposal_states?: Record<string, number>; provider_invoked?: boolean; canonical_preserved?: boolean }
}
export type MaintenanceStatus = {
  data_directory: string; plugin_directory: string; backup_directory: string; restore_directory: string
  backups: { id: string; name: string }[]; operations: MaintenanceOperation[]
}

export function consumeMaintenanceKey(): string {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('key') ?? ''
  if (window.location.hash) window.history.replaceState(null, '', window.location.pathname + window.location.search)
  return /^[A-Za-z0-9_-]{40,128}$/.test(token) ? token : ''
}

async function request<T>(token: string, path = '', body?: object, method = body ? 'POST' : 'GET'): Promise<T> {
  const response = await fetch(`/api/maintenance${path}`, {
    method, cache: 'no-store', credentials: 'omit', redirect: 'error',
    headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
  if (!response.ok) {
    const failure = await response.json().catch(() => null)
    throw new Error(typeof failure?.detail === 'string' && /^[a-z0-9_]{1,100}$/.test(failure.detail) ? failure.detail : 'maintenance_request_failed')
  }
  return response.json() as Promise<T>
}

export const getMaintenanceStatus = (token: string) => request<MaintenanceStatus>(token)
export const prepareMaintenance = (token: string, body: MaintenancePrepare) => request<MaintenanceOperation>(token, '/reviews', body)
export const confirmMaintenance = (token: string, operation: MaintenanceOperation) => request<MaintenanceOperation>(token, `/reviews/${encodeURIComponent(operation.request_id)}/confirm`, { fingerprint: operation.fingerprint, confirmed: true })
export const discardMaintenance = (token: string, operation: MaintenanceOperation) => request<MaintenanceOperation>(token, `/reviews/${encodeURIComponent(operation.request_id)}`, undefined, 'DELETE')

const messages: Record<string, string> = {
  maintenance_access_denied: 'This maintenance session is unavailable or expired. Restart the local console and open its new private link.',
  maintenance_directory_busy: 'The application or another maintenance operation is using this directory. Stop the application or wait for the operation, then retry.',
  finish_current_maintenance_review: 'Complete or discard the current review before preparing another copy.',
  maintenance_review_expired: 'This review expired. Discard it and prepare a new copy.',
  maintenance_review_changed: 'The review changed. Refresh maintenance state before continuing.',
  maintenance_prepared_copy_changed: 'The prepared copy changed and cannot be published. Discard this review and prepare it again.',
  maintenance_staging_changed: 'The private staging directory changed. Ask the local operator to inspect it before continuing.',
  maintenance_staging_missing: 'The prepared copy is missing. Ask the local operator to inspect the committed review.',
  maintenance_target_conflict: 'The destination is already occupied or the prepared copy is missing. Existing files were preserved.',
  target_exists: 'The destination is already occupied. Existing files were preserved.',
  implementation_changed: 'The application code changed. Use the matching application version and review again.',
  implementation_mismatch: 'This backup requires the same application version that created it.',
  maintenance_already_committed: 'This copy is already committed. Refresh or retry its publication to check the result.',
  backup_not_found: 'The selected backup is no longer available. Refresh the backup list.',
  nonterminal_run_present: 'Finish or cancel every conversation before making a complete backup.',
  pending_data_erasure: 'Finish the interrupted data deletion in the original application before backing it up.',
  file_inventory_mismatch: 'The backup files do not match their recorded checksums. Use an intact backup.',
  manifest_schema: 'This directory is not a supported complete application backup.',
  source_changed: 'The source changed during capture. Stop its writers and prepare another review.',
  maintenance_history_limit: 'This maintenance workspace has reached its history limit. Use a new private maintenance workspace.',
  maintenance_backup_limit: 'This backup folder has reached its entry limit. Use a new private maintenance workspace.',
  maintenance_response_mismatch: 'The response did not match this reviewed operation. Refresh its state before continuing.',
}
export function maintenanceError(code: string): string {
  return messages[code] ?? 'Maintenance could not be verified. Refresh its state and check the selected backup before retrying.'
}
