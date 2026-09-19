export const projectExportFormat = 'ophira-project-export-v1' as const

export type ProjectExportEntry = {
  path: string
  category: string
  size_bytes: number
  sha256: string
}

export type ProjectExportPreview = {
  format: typeof projectExportFormat
  project_id: string
  actor_id: string
  revision: number
  fingerprint: string
  file_count: number
  total_bytes: number
  directories: string[]
  entries: ProjectExportEntry[]
  notes: string[]
}
