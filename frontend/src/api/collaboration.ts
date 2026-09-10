export type ProjectRole = 'owner' | 'admin' | 'member' | 'reviewer' | 'viewer'
export type CollaborationCapabilities = {
  project_id: string
  identity_mode: 'local_only' | 'trusted_gateway'
  actor_id: string
  role: ProjectRole | null
  can_comment: boolean
  can_manage_members: boolean
  grantable_roles: Exclude<ProjectRole, 'owner'>[]
}
export type ProjectMembership = { principal_id: string; role: ProjectRole; granted_by: string; granted_at: string }
export type ProjectAccess = {
  project_id: string; tenant_id: string; owner_principal_id: string
  memberships: Record<string, ProjectMembership>; revision: number; created_at: string; updated_at: string
}
export type CommentAnchor = { kind: 'project' | 'run' | 'message' | 'evidence' | 'decision' | 'artifact'; id?: string | null; run_id?: string | null }
export type ProjectComment = {
  id: string; project_id: string; parent_id: string | null; sequence: number
  author_id: string; revision: number; text: string | null; state: 'open' | 'resolved'
  created_at: string; updated_at: string; reply_count: number
  target: { reference: CommentAnchor; label: string; trace_id: string | null; artifact_version: number | null; artifact_checksum: string | null }
  actions: { edit: boolean; remove: boolean; resolve: boolean; history: boolean }
}
export type CommentPage = { project_id: string; items: ProjectComment[]; next_before: number | null }
export type CommentThread = { project_id: string; root: ProjectComment; replies: ProjectComment[]; next_after: number | null }
export type CommentCreate = { request_id: string; text: string; anchor?: CommentAnchor; parent_id?: string }
export type CommentChange = { request_id: string; expected_revision: number; action: 'edit' | 'remove' | 'resolve' | 'reopen'; text?: string }
export type CommentHistory = {
  project_id: string; comment_id: string
  revisions: { revision: number; actor_id: string; action: 'create' | CommentChange['action']; text: string | null; state: 'open' | 'resolved'; at: string }[]
}

export function collaborationAccessLost(reason: unknown): boolean {
  return !!reason && typeof reason === 'object' && 'status' in reason && [401, 403, 404].includes(Number(reason.status))
}

export function requireCollaborationScope(projectId: string, ...rows: { project_id: string }[]) {
  if (rows.some((row) => row.project_id !== projectId)) throw new Error('Collaboration response did not match this project.')
}
