export interface ProjectExportPreview {
  schema_version: 1
  kind: 'project' | 'session'
  project_id: string
  selected_run_id: string | null
  project_revision: number
  run_ids: string[]
  fingerprint: string
  file_count: number
  total_bytes: number
  warnings: { code: string; id: string }[]
  exclusions: string[]
}
