import type { RunStatus, SkillTestResult } from './types'

export type SkillProposalState = 'pending' | 'publishing' | 'published' | 'rejected'

export const proposalStateLabels: Record<SkillProposalState, string> = {
  pending: 'Pending review', publishing: 'Publishing', published: 'Published', rejected: 'Rejected',
}

export type SkillSelectionToken = {
  current_hash: string | null
  enabled: boolean | null
  generation: number
}

export type SkillProposalManifest = {
  schema_version: 1
  kind: 'ophira.skill-proposal'
  name: string
  version: string
  package_dir: 'package'
  reason: string
  related_run_ids: string[]
}

export type SkillProposalSummary = {
  id: string
  revision: number
  state: SkillProposalState
  project_id: string
  project_name: string
  source_run_id: string
  source_artifact_id: string
  skill_id: string
  manifest: SkillProposalManifest
  description: string
  content_hash: string
  scan_ok: boolean
  created_at: string
  updated_at: string
}

export type SkillProposalFile = {
  path: string
  content_hash: string
  size_bytes: number
  preview_kind: 'text' | 'binary'
}

export type SkillProposalReviewRequest = {
  decision: 'approve' | 'reject'
  revision: number
  content_hash: string
  expected_target?: SkillSelectionToken
  note: string
}

export type SkillProposalReview = {
  review_revision: number
  decision: 'approve' | 'reject'
  content_hash: string
  expected_target: SkillSelectionToken | null
  reviewer: { kind: 'local_owner' | 'principal'; id: string; role: 'owner' | 'admin' }
  note: string
  reviewed_at: string
  reviewer_binding: string
  outcome: 'publishing' | 'published' | 'rejected' | 'interrupted'
}

export type SkillProposalDetail = Omit<SkillProposalSummary, 'scan_ok'> & {
  source_sdk_session_id: string
  manifest_checksum: string
  files: SkillProposalFile[]
  scan: SkillTestResult
  scan_content_hash: string
  source_runs: Array<{
    id: string
    status: RunStatus
    prompt_excerpt: string
    started_at: string
    finished_at: string | null
  }>
  review: SkillProposalReview | null
  history: SkillProposalReview[]
  publication_receipt: {
    proposal_id: string
    skill_id: string
    content_hash: string
    review_revision: number
    reviewer_binding: string
    previous_selection: SkillSelectionToken
    published_at: string
  } | null
  target: SkillSelectionToken
  actions: { can_publish: boolean; can_reject: boolean; reason: string | null }
}

export type SkillProposalPage = {
  items: SkillProposalSummary[]
  next_cursor: string | null
}

export type SkillProposalPreview = {
  proposal_id: string
  content_hash: string
  path: string
  file_content_hash: string
  size_bytes: number
  preview_kind: 'text' | 'binary'
  text: string | null
  complete: boolean
}
