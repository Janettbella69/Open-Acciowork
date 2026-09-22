import type { SkillProposalDetail, SkillProposalFile, SkillProposalReview, SkillProposalReviewRequest, SkillProposalState, SkillSelectionToken } from './skillProposals'
import type { SkillTestResult, SkillVersion } from './types'

export type SkillRepositoryCapabilities = { identity_mode: 'local_only' | 'trusted_gateway'; tenant_id: string | null; actor_id: string; can_manage: boolean }
export type SkillImport = {
  id: string; request_id: string; archive_hash: string; filename: string
  scope: { schema_version: 1; auth_mode: 'local_only' | 'trusted_gateway'; tenant_id: string | null }
  uploaded_by: SkillProposalReview['reviewer']; skill: SkillVersion; files: SkillProposalFile[]; scan: SkillTestResult
  published_before_import: boolean
  revision: number; state: SkillProposalState; created_at: string; updated_at: string
  history: Array<SkillProposalReview & { request_id: string; request_hash: string }>
  publication_receipt: SkillProposalDetail['publication_receipt']
}
export type SkillImportView = { import: SkillImport; current_target: SkillSelectionToken; can_publish: boolean }
export type SkillImportReviewRequest = SkillProposalReviewRequest & { request_id: string }
export type SkillPackageView = { skill: SkillVersion; files: SkillProposalFile[]; current_target: SkillSelectionToken }
export type SkillFilePreview = { path: string; content_hash: string; size_bytes: number; text: string | null; truncated: boolean }
export type SkillRepositorySelection = { operation: 'enable' | 'disable' | 'rollback'; content_hash: string; expected_target: SkillSelectionToken }

export function requireSkillImportScope(caps: SkillRepositoryCapabilities, value: SkillImportView, identifier?: string) {
  const item = value.import
  if (!item || item.scope.auth_mode !== caps.identity_mode || item.scope.tenant_id !== caps.tenant_id || (identifier && item.id !== identifier)
    || item.skill.source !== 'tenant' || item.skill.namespace !== 'ophira-tenant') throw new Error('Private Skill response did not match this repository.')
}
export function requireSkillPackage(skillId: string, hash: string, value: SkillPackageView) {
  if (value.skill.id !== skillId || value.skill.content_hash !== hash || value.skill.source !== 'tenant') throw new Error('Private Skill response did not match the selected version.')
}
