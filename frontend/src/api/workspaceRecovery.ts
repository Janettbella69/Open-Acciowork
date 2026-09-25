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
  archived_at?: string | null
}

export type RetentionCheckpoint = {
  id: string
  run_id: string | null
  kind: WorkspaceCheckpoint['kind']
  created_at: string
  files: number
  total_bytes: number
  fingerprint: string
  referenced_by_reviews: number
}

export type RetentionReview = {
  id: string
  checkpoint_id: string
  state: WorkspaceRestore['state']
  created_at: string
  expires_at: string
  fingerprint: string
  retained_bytes: number
  retained_discarded_at: string | null
  can_delete: boolean
  can_archive: boolean
}

export type WorkspaceRetention = {
  project_id: string
  checkpoint_limit: number
  review_limit: number
  retained_bytes_limit: number
  retained_bytes: number
  archived_reviews: number
  snapshot_files: number
  snapshot_bytes: number
  reclaimable_files: number
  reclaimable_bytes: number
  reclaim_fingerprint: string
  checkpoints: RetentionCheckpoint[]
  reviews: RetentionReview[]
}
