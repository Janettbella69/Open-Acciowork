import type { SkillRepositoryCapabilities } from './skillRepository'

export type PluginImportedSkill = {
  path: string
  import_id: string
  skill_id: string
  content_hash: string
  scan_ok: boolean
}

export type PluginImportRecord = {
  schema_version: number
  id: string
  request_id: string
  archive_hash: string
  scope: { auth_mode: 'local_only' | 'trusted_gateway'; tenant_id: string | null }
  uploaded_by: { kind: string; id: string; role: string }
  name: string
  version: string | null
  description: string | null
  skills: PluginImportedSkill[]
  mcp_servers: Array<{ name: string; url: string; credentials_required: boolean }>
  ignored_agents: string[]
  ignored_components: string[]
  created_at: string
}

export type PluginImportView = {
  import: PluginImportRecord
  skill_states: Array<{
    path: string
    import_id: string
    skill_id: string
    state: 'pending' | 'publishing' | 'published' | 'rejected'
    scan_ok: boolean
  }>
}

export function requirePluginImportScope(
  capabilities: SkillRepositoryCapabilities, record: PluginImportRecord, identifier?: string,
): void {
  if (
    record.scope.auth_mode !== capabilities.identity_mode
    || record.scope.tenant_id !== capabilities.tenant_id
    || (identifier && record.id !== identifier)
  ) throw new Error('Plugin import response did not match this repository.')
}
