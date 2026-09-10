import type { OpportunityProject } from './types'

export type ProjectSeed = Pick<OpportunityProject, 'name' | 'objective' | 'product_brief' | 'target_markets' | 'constraints'>
export type TemplateReference = { artifact_id: string; name: string; mime: string; version: number; checksum: string; size_bytes: number }
export type TemplateVersionSummary = { version: number; title: string; content_hash: string; created_by: string; created_at: string; file_count: number; size_bytes: number }
export type ProjectTemplate = {
  id: string; project_id: string; created_by: string; revision: number; archived: boolean
  title: string; description: string; latest_version: number; content_hash: string
  created_at: string; updated_at: string; can_manage: boolean; can_apply: boolean
  versions: TemplateVersionSummary[]
}
export type TemplateLibrary = { project_id: string; actor_id: string; can_save: boolean; can_create_project: boolean; items: ProjectTemplate[] }
export type TemplateVersion = {
  version: number; title: string; description: string; seed: ProjectSeed; references: TemplateReference[]
  source_project_revision: number; created_by: string; created_at: string; content_hash: string
}
export type TemplateDetail = { project_id: string; template: ProjectTemplate; version: TemplateVersion }
export type TemplateSave = { request_id: string; title: string; description: string; seed: ProjectSeed; artifact_ids: string[]; expected_revision?: number }
export type TemplateApply = { request_id: string; version: number; content_hash: string; seed: ProjectSeed; artifact_ids: string[] }
export type TemplateApplication = {
  id: string; source_project_id: string; template_id: string; template_version: number; project_id: string
  state: 'preparing' | 'ready' | 'applied'; created_at: string; created_by: string; seed: ProjectSeed
  references: TemplateReference[]; can_resume: boolean
}
export type TemplateApplications = { project_id: string; template_id: string; items: TemplateApplication[]; next_before: string | null }
export type TemplateApplied = { project: OpportunityProject; application: TemplateApplication }
export type ProjectTemplateOrigin = {
  source_project_id: string; template_id: string; version: number; content_hash: string; seed_hash: string
  title: string; application_id: string; applied_by: string; created_at: string
  references: { artifact_id: string; source: TemplateReference }[]
}

export function requireTemplateDetail(projectId: string, templateId: string, version: number, detail: TemplateDetail) {
  if (detail.project_id !== projectId || detail.template.project_id !== projectId || detail.template.id !== templateId
    || detail.version.version !== version || !detail.template.versions.some((row) => row.version === version && row.content_hash === detail.version.content_hash)) {
    throw new Error('Template response did not match the reviewed version.')
  }
}
export function requireTemplateApplication(projectId: string, templateId: string, value: TemplateApplied) {
  const { application, project } = value
  if (application.source_project_id !== projectId || application.template_id !== templateId || application.state !== 'applied'
    || project.id !== application.project_id || project.template_origin?.application_id !== application.id
    || project.template_origin.source_project_id !== projectId || project.template_origin.template_id !== templateId) {
    throw new Error('Template response did not match the reviewed project creation.')
  }
}
