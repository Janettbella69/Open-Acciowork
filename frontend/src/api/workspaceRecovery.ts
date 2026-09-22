export type WorkspaceCheckpoint = {
  id: string
  project_id: string
  run_id: string | null
  kind: 'run_start' | 'before_restore'
  created_at: string
  files: number
  total_bytes: number
}

export type WorkspaceChange = {
  path: string
  action: 'restore' | 'replace' | 'remove'
  before_kind: 'file' | 'directory' | null
  after_kind: 'file' | 'directory' | null
}

export type WorkspaceRestore = {
  id: string
  project_id: string
  checkpoint_id: string
  created_at: string
  expires_at: string
  state: 'preview' | 'applying' | 'applied'
  changes: WorkspaceChange[]
  removes_files: number
  safety_checkpoint_id: string | null
  applied_at: string | null
}
