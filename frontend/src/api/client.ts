import type {
  AgentProfile,
  AgentTemplate,
  Artifact,
  Connector,
  Evidence,
  EvidenceStatus,
  EvidenceStatusChange,
  MemoryItem,
  MemoryKind,
  MemorySensitivity,
  OpportunityProject,
  ProtocolMetadata,
  ProviderPreflightResult,
  ProviderProfile,
  RunMode,
  RunRecord,
  RunRetryStrategy,
  RunTelemetry,
  Schedule,
  ScheduleKind,
  SchedulePermissionRule,
  SkillTestResult,
  SkillVersion,
  StandingIntent,
  Notification,
  OrgPolicy,
  PermissionGrant,
  PendingPermissionView,
  FeedbackCreate,
  FeedbackItem,
  FeedbackReview,
  FeedbackStatus,
  CommercialActionRecord,
  CommercialReviewDecision,
  TaskExperienceLedger,
} from './types'
import type { ProjectMetrics } from './projectMetrics'
import type { SkillProposalDetail, SkillProposalPage, SkillProposalPreview, SkillProposalReviewRequest, SkillProposalState } from './skillProposals'
import type { KnowledgeSearch } from './knowledge'
import type { CredentialArchive, CredentialView } from './credentials'
import type { MarketplaceAccess, MarketplaceGuide, MarketplaceRead, MarketplaceReadInput } from './marketplaces'
import type { ProjectShop, ShopData, ShopIdentityInput } from './shops'
import type { ArtifactTextUpdate, ArtifactTextView } from './artifacts'
import type { WorkspaceCheckpoint, WorkspaceRestore } from './workspaceRecovery'
import type { RunResources } from './runResources'
import type { CollaborationCapabilities, CommentChange, CommentCreate, CommentHistory, CommentPage, CommentThread, ProjectAccess, ProjectComment, ProjectRole } from './collaboration'
import type { ProjectTemplate, TemplateApplied, TemplateApplications, TemplateApply, TemplateDetail, TemplateLibrary, TemplateSave } from './projectTemplates'
import type { SkillFilePreview, SkillImportReviewRequest, SkillImportView, SkillPackageView, SkillRepositoryCapabilities, SkillRepositorySelection } from './skillRepository'

const configuredBaseUrl = import.meta.env.VITE_OPHIRA_API_URL?.trim() ?? ''
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '')

export function listCredentials(signal?: AbortSignal): Promise<CredentialView[]> {
  return request('/api/credentials', { signal, cache: 'no-store' })
}
export function exportCredential(item: CredentialView, password: string, signal?: AbortSignal): Promise<CredentialArchive> {
  return request(`/api/credentials/${item.kind}/${encodeURIComponent(item.id)}/export`, {
    method: 'POST', signal, cache: 'no-store', body: JSON.stringify({ expected_revision: item.revision, password }),
  })
}
export function deleteCredential(item: CredentialView, signal?: AbortSignal): Promise<CredentialView> {
  return request(`/api/credentials/${item.kind}/${encodeURIComponent(item.id)}`, {
    method: 'DELETE', signal, cache: 'no-store', body: JSON.stringify({ expected_revision: item.revision, confirm_delete: true }),
  })
}

function templatePath(projectId: string, templateId?: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/templates${templateId ? `/${encodeURIComponent(templateId)}` : ''}`
}
export function listProjectTemplates(projectId: string, signal?: AbortSignal): Promise<TemplateLibrary> {
  return request(templatePath(projectId), { signal, cache: 'no-store' })
}
export function getTemplateVersion(projectId: string, templateId: string, version: number, signal?: AbortSignal): Promise<TemplateDetail> {
  return request(`${templatePath(projectId, templateId)}/versions/${version}`, { signal, cache: 'no-store' })
}
export function saveProjectTemplate(projectId: string, payload: TemplateSave, templateId?: string, signal?: AbortSignal): Promise<ProjectTemplate> {
  return request(`${templatePath(projectId, templateId)}${templateId ? '/versions' : ''}`, { method: 'POST', body: JSON.stringify(payload), signal, cache: 'no-store' })
}
export function archiveProjectTemplate(projectId: string, templateId: string, payload: { request_id: string; expected_revision: number; archived: boolean }, signal?: AbortSignal): Promise<ProjectTemplate> {
  return request(`${templatePath(projectId, templateId)}/archive`, { method: 'POST', body: JSON.stringify(payload), signal, cache: 'no-store' })
}
export function templateReferenceUrl(projectId: string, templateId: string, version: number, artifactId: string): string {
  return `${apiBaseUrl}${templatePath(projectId, templateId)}/versions/${version}/references/${encodeURIComponent(artifactId)}/download`
}
export function applyProjectTemplate(projectId: string, templateId: string, payload: TemplateApply, signal?: AbortSignal): Promise<TemplateApplied> {
  return request(`${templatePath(projectId, templateId)}/applications`, { method: 'POST', body: JSON.stringify(payload), signal, cache: 'no-store' })
}
export function listTemplateApplications(projectId: string, templateId: string, before?: string, signal?: AbortSignal): Promise<TemplateApplications> {
  return request(`${templatePath(projectId, templateId)}/applications${before ? `?before=${encodeURIComponent(before)}` : ''}`, { signal, cache: 'no-store' })
}
export function resumeTemplateApplication(projectId: string, templateId: string, applicationId: string, signal?: AbortSignal): Promise<TemplateApplied> {
  return request(`${templatePath(projectId, templateId)}/applications/${encodeURIComponent(applicationId)}/resume`, { method: 'POST', body: JSON.stringify({ confirm_creation: true }), signal, cache: 'no-store' })
}

export function getCollaborationCapabilities(projectId: string, signal?: AbortSignal): Promise<CollaborationCapabilities> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/collaboration`, { signal, cache: 'no-store' })
}
export function getProjectAccess(projectId: string, signal?: AbortSignal): Promise<ProjectAccess> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/access`, { signal, cache: 'no-store' })
}
export function grantProjectAccess(projectId: string, payload: { principal_id: string; role: Exclude<ProjectRole, 'owner'>; expected_revision: number }, signal?: AbortSignal): Promise<ProjectAccess> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/access`, { method: 'POST', body: JSON.stringify(payload), signal })
}
export function revokeProjectAccess(projectId: string, principalId: string, revision: number, signal?: AbortSignal): Promise<ProjectAccess> {
  // The signed gateway accepts canonical ASCII paths, without percent escapes.
  // These three identity characters are safe verbatim within this path segment.
  const account = encodeURIComponent(principalId).replace(/%40/g, '@').replace(/%3A/g, ':').replace(/%2B/g, '+')
  return request(`/api/projects/${encodeURIComponent(projectId)}/access/${account}?expected_revision=${revision}`, { method: 'DELETE', signal })
}
export function listProjectComments(projectId: string, before?: number, signal?: AbortSignal): Promise<CommentPage> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/comments${before === undefined ? '' : `?before=${before}`}`, { signal, cache: 'no-store' })
}
export function getCommentThread(projectId: string, commentId: string, after?: number, signal?: AbortSignal): Promise<CommentThread> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/comments/${encodeURIComponent(commentId)}/thread${after === undefined ? '' : `?after=${after}`}`, { signal, cache: 'no-store' })
}
export function createProjectComment(projectId: string, payload: CommentCreate, signal?: AbortSignal): Promise<ProjectComment> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/comments`, { method: 'POST', body: JSON.stringify(payload), signal })
}
export function changeProjectComment(projectId: string, commentId: string, payload: CommentChange, signal?: AbortSignal): Promise<ProjectComment> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/comments/${encodeURIComponent(commentId)}`, { method: 'PATCH', body: JSON.stringify(payload), signal })
}
export function getCommentHistory(projectId: string, commentId: string, signal?: AbortSignal): Promise<CommentHistory> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/comments/${encodeURIComponent(commentId)}/history`, { signal, cache: 'no-store' })
}

export function artifactDownloadUrl(projectId: string, artifactId: string): string {
  return `${apiBaseUrl}/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/download`
}

export function getArtifactText(projectId: string, artifactId: string, signal?: AbortSignal): Promise<ArtifactTextView> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/text`, { signal })
}

export function saveArtifactText(projectId: string, artifactId: string, payload: ArtifactTextUpdate, signal?: AbortSignal): Promise<Artifact> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/text`, {
    method: 'PUT', body: JSON.stringify(payload), signal,
  })
}

export function listWorkspaceCheckpoints(projectId: string, signal?: AbortSignal): Promise<WorkspaceCheckpoint[]> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/workspace/checkpoints`, { signal })
}

export function previewWorkspaceRestore(projectId: string, checkpointId: string, signal?: AbortSignal): Promise<WorkspaceRestore> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/workspace/restores`, {
    method: 'POST', body: JSON.stringify({ checkpoint_id: checkpointId }), signal,
  })
}

export function applyWorkspaceRestore(projectId: string, restoreId: string, signal?: AbortSignal): Promise<WorkspaceRestore> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/workspace/restores/${encodeURIComponent(restoreId)}/apply`, {
    method: 'POST', body: JSON.stringify({ confirm_file_changes: true }), signal,
  })
}

export function getWorkspaceRestore(projectId: string, restoreId: string, signal?: AbortSignal): Promise<WorkspaceRestore> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/workspace/restores/${encodeURIComponent(restoreId)}`, { signal })
}

class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as { detail?: unknown }
      message = apiDetailMessage(body.detail) ?? message
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function apiDetailMessage(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (!Array.isArray(detail)) return null
  const messages = detail.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const issue = item as { loc?: unknown; msg?: unknown }
    if (typeof issue.msg !== 'string' || !issue.msg.trim()) return []
    const location = Array.isArray(issue.loc)
      ? issue.loc.filter((part) => typeof part === 'string' || typeof part === 'number').join('.')
      : ''
    return [location ? `${location}: ${issue.msg}` : issue.msg]
  })
  return messages.length ? messages.join('; ') : null
}

let protocolPromise: Promise<ProtocolMetadata> | null = null

export function getProtocol(): Promise<ProtocolMetadata> {
  protocolPromise ??= request<ProtocolMetadata>('/api/meta/protocol').catch((error: unknown) => {
    protocolPromise = null
    throw error
  })
  return protocolPromise
}

export function listProjects(): Promise<OpportunityProject[]> {
  return request<OpportunityProject[]>('/api/projects')
}

export function listAgents(): Promise<AgentProfile[]> {
  return request<AgentProfile[]>('/api/agents')
}

export function createAgent(input: {
  name: string
  template: AgentTemplate
  role_description?: string
  model?: string | null
}): Promise<AgentProfile> {
  return request<AgentProfile>('/api/agents', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function createProject(brief: string): Promise<OpportunityProject> {
  return request<OpportunityProject>('/api/projects', {
    method: 'POST',
    body: JSON.stringify({
      name: brief.slice(0, 120),
      objective: brief,
      product_brief: brief,
      target_markets: [],
      constraints: [],
    }),
  })
}

export function getProject(projectId: string, signal?: AbortSignal): Promise<OpportunityProject> {
  return request<OpportunityProject>(`/api/projects/${encodeURIComponent(projectId)}`, {
    signal,
    cache: 'no-store',
  })
}

export function listProjectRuns(projectId: string): Promise<RunRecord[]> {
  return request<RunRecord[]>(`/api/projects/${projectId}/runs`)
}

export function getProjectKnowledge(projectId: string, query = '', signal?: AbortSignal): Promise<KnowledgeSearch> {
  const params = new URLSearchParams({ q: query, limit: '30' })
  return request<KnowledgeSearch>(`/api/projects/${encodeURIComponent(projectId)}/knowledge?${params}`, {
    signal, cache: 'no-store',
  })
}

export function startRun(
  projectId: string,
  prompt: string,
  resumeSessionId?: string | null,
  mode: RunMode = 'standard',
  providerProfileId?: string | null,
  agentId?: string | null,
): Promise<RunRecord> {
  return request<RunRecord>(`/api/projects/${projectId}/runs`, {
    method: 'POST',
    body: JSON.stringify({
      prompt,
      mode,
      ...(resumeSessionId ? { resume_session_id: resumeSessionId } : {}),
      ...(providerProfileId ? { provider_profile_id: providerProfileId } : {}),
      ...(agentId ? { agent_id: agentId } : {}),
    }),
  })
}

export function retryRun(runId: string, strategy: RunRetryStrategy): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${encodeURIComponent(runId)}/retry`, {
    method: 'POST',
    body: JSON.stringify({ strategy }),
  })
}

export function uploadProjectFile(projectId: string, file: File): Promise<Artifact> {
  const params = new URLSearchParams({ filename: file.name })
  if (file.type) params.set('mime', file.type)
  return request<Artifact>(`/api/projects/${projectId}/files?${params}`, {
    method: 'POST',
    body: file,
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
  })
}

export function listProjectArtifacts(projectId: string): Promise<Artifact[]> {
  return request<Artifact[]>(`/api/projects/${projectId}/artifacts`)
}

export function listProviders(): Promise<ProviderProfile[]> {
  return request<ProviderProfile[]>('/api/providers')
}

export type ProviderInput = {
  display_name: string
  base_url: string | null
  auth_env_name: 'ANTHROPIC_API_KEY' | 'ANTHROPIC_AUTH_TOKEN'
  model: string
  secret?: string
  region?: string
  production_eligible?: boolean
  // The backend upsert replaces the whole profile, so the editor must round-trip
  // the tier mappings and capability matrix or a save would silently reset them.
  default_haiku_model?: string | null
  default_sonnet_model?: string | null
  default_opus_model?: string | null
  subagent_model?: string | null
  budget_scale?: number
  terms_checked_at?: string | null
  capabilities?: import('./types').ProviderCapabilities
}

export function saveProvider(providerId: string, input: ProviderInput): Promise<ProviderProfile> {
  return request<ProviderProfile>(`/api/providers/${encodeURIComponent(providerId)}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export function preflightProvider(providerId: string): Promise<ProviderPreflightResult> {
  return request<ProviderPreflightResult>(
    `/api/providers/${encodeURIComponent(providerId)}/preflight`,
    { method: 'POST' },
  )
}

export function listProjectEvidence(
  projectId: string,
  params: { q?: string; kind?: string; status?: string; tag?: string; limit?: number } = {},
): Promise<Evidence[]> {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const suffix = search.toString() ? `?${search}` : ''
  return request<Evidence[]>(`/api/projects/${projectId}/evidence${suffix}`)
}

export function setEvidenceStatus(
  projectId: string,
  evidenceId: string,
  status: EvidenceStatus,
): Promise<EvidenceStatusChange> {
  return request<EvidenceStatusChange>(
    `/api/projects/${projectId}/evidence/${evidenceId}/status`,
    { method: 'POST', body: JSON.stringify({ status }) },
  )
}

function skillPathId(skillId: string): string {
  // A colon is canonical inside the namespaced Skill ID; the signed gateway
  // rejects percent-escaped route identities rather than accepting aliases.
  return encodeURIComponent(skillId).replace(/%3A/g, ':')
}

export function testSkill(skillId: string): Promise<SkillTestResult> {
  return request<SkillTestResult>(`/api/skills/${skillPathId(skillId)}/test`, {
    method: 'POST',
  })
}

export function listConnectors(): Promise<Connector[]> {
  return request<Connector[]>('/api/connectors')
}

export function checkConnectorHealth(connectorId: string): Promise<Connector> {
  return request<Connector>(`/api/connectors/${encodeURIComponent(connectorId)}/health-check`, {
    method: 'POST',
  })
}

export function patchSchedule(
  scheduleId: string,
  changes: Partial<{
    prompt: string
    cron: string | null
    run_once_at: string | null
    timezone: string
    missed_run_policy: 'catch_up' | 'skip'
    provider_profile_id: string | null
    permission_ceiling: SchedulePermissionRule[] | null
    mode: RunMode
    enabled: boolean
  }>,
): Promise<Schedule> {
  return request<Schedule>(`/api/schedules/${scheduleId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function listSkills(): Promise<SkillVersion[]> {
  return request<SkillVersion[]>('/api/skills')
}

export function listSkillProposals(state: SkillProposalState, cursor?: string | null, signal?: AbortSignal): Promise<SkillProposalPage> {
  const query = new URLSearchParams({ state, limit: '50' })
  if (cursor) query.set('cursor', cursor)
  return request<SkillProposalPage>(`/api/skill-proposals?${query}`, { signal, cache: 'no-store' })
}

export function getSkillProposal(id: string, signal?: AbortSignal): Promise<SkillProposalDetail> {
  return request<SkillProposalDetail>(`/api/skill-proposals/${encodeURIComponent(id)}`, { signal, cache: 'no-store' })
}

export function getSkillProposalFile(id: string, path: string, contentHash: string, signal?: AbortSignal): Promise<SkillProposalPreview> {
  const query = new URLSearchParams({ path, content_hash: contentHash })
  return request<SkillProposalPreview>(`/api/skill-proposals/${encodeURIComponent(id)}/files?${query}`, { signal, cache: 'no-store' })
}

export function skillProposalDownloadUrl(id: string, path: string, contentHash: string): string {
  const query = new URLSearchParams({ path, content_hash: contentHash })
  return apiUrl(`/api/skill-proposals/${encodeURIComponent(id)}/download?${query}`)
}

export function reviewSkillProposal(id: string, review: SkillProposalReviewRequest): Promise<SkillProposalDetail> {
  return request<SkillProposalDetail>(`/api/skill-proposals/${encodeURIComponent(id)}/review`, {
    method: 'POST', body: JSON.stringify(review),
  })
}

async function reviewedPrivatePackage(skillId: string, expected: Pick<SkillVersion, 'content_hash' | 'enabled'>): Promise<SkillPackageView> {
  const value = await getSkillPackage(skillId.slice('ophira-tenant:'.length), expected.content_hash)
  if (value.skill.id !== skillId || value.skill.content_hash !== expected.content_hash
    || value.current_target.current_hash !== expected.content_hash || value.current_target.enabled !== expected.enabled) {
    throw new Error('Skill selection changed since review')
  }
  return value
}

export async function setSkillEnabled(skillId: string, enabled: boolean, expected?: Pick<SkillVersion, 'content_hash' | 'enabled'>): Promise<SkillVersion> {
  if (expected && skillId.startsWith('ophira-tenant:')) {
    const view = await reviewedPrivatePackage(skillId, expected)
    return (await selectPrivateSkill(view.skill.name, {
      operation: enabled ? 'enable' : 'disable', content_hash: expected.content_hash,
      expected_target: view.current_target,
    })).skill
  }
  return request<SkillVersion>(
    `/api/skills/${skillPathId(skillId)}/enable?enabled=${enabled}`,
    { method: 'POST' },
  )
}

export function listSkillVersions(skillId: string, signal?: AbortSignal): Promise<SkillVersion[]> {
  return request<SkillVersion[]>(`/api/skills/${skillPathId(skillId)}/versions`, { signal, cache: 'no-store' })
}

export async function rollbackSkill(
  skillId: string,
  selection: { version?: string; content_hash?: string },
  expected?: Pick<SkillVersion, 'content_hash' | 'enabled'>,
): Promise<SkillVersion> {
  if (expected && selection.content_hash && skillId.startsWith('ophira-tenant:')) {
    const view = await reviewedPrivatePackage(skillId, expected)
    return (await selectPrivateSkill(view.skill.name, {
      operation: 'rollback', content_hash: selection.content_hash, expected_target: view.current_target,
    })).skill
  }
  return request<SkillVersion>(`/api/skills/${skillPathId(skillId)}/rollback`, {
    method: 'POST',
    body: JSON.stringify(selection),
  })
}

export function listSchedules(): Promise<Schedule[]> {
  return request<Schedule[]>('/api/schedules')
}

export function createSchedule(input: {
  project_id: string
  prompt: string
  cron?: string
  run_once_at?: string
  timezone: string
  mode?: RunMode
  kind: ScheduleKind
  provider_profile_id?: string | null
  permission_ceiling: SchedulePermissionRule[]
}): Promise<Schedule> {
  return request<Schedule>('/api/schedules', { method: 'POST', body: JSON.stringify(input) })
}

export function pauseSchedule(scheduleId: string): Promise<Schedule> {
  return request<Schedule>(`/api/schedules/${scheduleId}/pause`, { method: 'POST' })
}

export function resumeSchedule(scheduleId: string): Promise<Schedule> {
  return request<Schedule>(`/api/schedules/${scheduleId}/resume`, { method: 'POST' })
}

export function runSchedule(scheduleId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/schedules/${scheduleId}/run`, { method: 'POST' })
}

export function listMemory(sourceRunId?: string, signal?: AbortSignal): Promise<MemoryItem[]> {
  const query = sourceRunId ? `?source_run_id=${encodeURIComponent(sourceRunId)}` : ''
  return request<MemoryItem[]>(`/api/memory${query}`, { signal, cache: 'no-store' })
}

export function decideMemory(
  memoryId: string,
  decision: 'approve' | 'reject' | 'disable',
): Promise<MemoryItem> {
  return request<MemoryItem>(`/api/memory/${memoryId}/decision`, {
    method: 'POST',
    body: JSON.stringify({ decision, reason: 'Updated by the user in Settings.' }),
  })
}

export function correctMemory(
  memoryId: string,
  input: { text: string; kind?: MemoryKind; sensitivity?: MemorySensitivity },
): Promise<MemoryItem> {
  return request<MemoryItem>(`/api/memory/${encodeURIComponent(memoryId)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  })
}

export function getRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${runId}`)
}

export function getRunTelemetry(runId: string): Promise<RunTelemetry> {
  return request<RunTelemetry>(`/api/runs/${encodeURIComponent(runId)}/telemetry`)
}

export function getRunResources(runId: string, signal?: AbortSignal): Promise<RunResources> {
  return request(`/api/runs/${encodeURIComponent(runId)}/resources`, { signal })
}

export function getProjectMetrics(projectId: string, days: number, signal?: AbortSignal): Promise<ProjectMetrics> {
  return request<ProjectMetrics>(`/api/projects/${encodeURIComponent(projectId)}/metrics?days=${days}`, { signal })
}

export function cancelRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${runId}/cancel`, { method: 'POST' })
}

export function resolvePermission(
  runId: string,
  requestId: string,
  decision: 'allow_once' | 'allow_run' | 'allow_persistent' | 'deny',
  answers?: Record<string, string | string[]>,
): Promise<void> {
  return request<void>(`/api/runs/${runId}/permissions/${requestId}`, {
    method: 'POST',
    body: JSON.stringify({
      decision,
      reason: decision === 'deny' ? 'Denied by the user.' : '',
      ...(answers ? { answers } : {}),
    }),
  })
}

export function resumePermissionRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${encodeURIComponent(runId)}/resume-permission`, { method: 'POST' })
}

export function listNotifications(unread = false): Promise<Notification[]> {
  return request<Notification[]>(`/api/notifications${unread ? '?unread=true' : ''}`)
}

export function markNotificationRead(id: string): Promise<Notification> {
  return request<Notification>(`/api/notifications/${id}/read`, { method: 'POST' })
}

export function markAllNotificationsRead(): Promise<{ marked: number }> {
  return request<{ marked: number }>('/api/notifications/read-all', { method: 'POST' })
}

export function listIntents(projectId?: string): Promise<StandingIntent[]> {
  const suffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return request<StandingIntent[]>(`/api/intents${suffix}`)
}

export function createIntent(input: {
  project_id: string
  intent: string
  cooldown_hours?: number
  max_triggers?: number
  expires_days?: number
}): Promise<StandingIntent> {
  return request<StandingIntent>('/api/intents', { method: 'POST', body: JSON.stringify(input) })
}

export function pauseIntent(id: string): Promise<StandingIntent> {
  return request<StandingIntent>(`/api/intents/${id}/pause`, { method: 'POST' })
}

export function resumeIntent(id: string): Promise<StandingIntent> {
  return request<StandingIntent>(`/api/intents/${id}/resume`, { method: 'POST' })
}

export function getOrgPolicy(): Promise<OrgPolicy> {
  return request<OrgPolicy>('/api/org/policy')
}

export function saveOrgPolicy(policy: OrgPolicy): Promise<OrgPolicy> {
  return request<OrgPolicy>('/api/org/policy', { method: 'PUT', body: JSON.stringify(policy) })
}

export function listPendingPermissions(): Promise<PendingPermissionView[]> {
  return request<PendingPermissionView[]>('/api/permissions/pending')
}

export function listPermissionGrants(): Promise<PermissionGrant[]> {
  return request<PermissionGrant[]>('/api/permissions/grants')
}

export function revokePermissionGrant(id: string): Promise<PermissionGrant> {
  return request<PermissionGrant>(`/api/permissions/grants/${id}/revoke`, { method: 'POST' })
}

export function submitFeedback(input: FeedbackCreate): Promise<FeedbackItem> {
  return request<FeedbackItem>('/api/feedback', { method: 'POST', body: JSON.stringify(input) })
}

export function listFeedback(projectId: string, status?: FeedbackStatus, offset = 0): Promise<FeedbackItem[]> {
  const query = new URLSearchParams({ project_id: projectId, limit: '200', offset: String(offset) })
  if (status) query.set('status', status)
  return request<FeedbackItem[]>(`/api/feedback?${query}`)
}

export function reviewFeedback(id: string, input: FeedbackReview): Promise<FeedbackItem> {
  return request<FeedbackItem>(`/api/feedback/${encodeURIComponent(id)}/review`, {
    method: 'POST', body: JSON.stringify(input),
  })
}

export function getCommercialAction(
  projectId: string,
  actionId: string,
): Promise<CommercialActionRecord> {
  return request<CommercialActionRecord>(
    `/api/projects/${encodeURIComponent(projectId)}/commercial-actions/${encodeURIComponent(actionId)}`,
  )
}

export function reviewCommercialAction(
  projectId: string,
  actionId: string,
  decision: CommercialReviewDecision,
  reason: string,
): Promise<CommercialActionRecord> {
  return request<CommercialActionRecord>(
    `/api/projects/${encodeURIComponent(projectId)}/commercial-actions/${encodeURIComponent(actionId)}/review`,
    {
      method: 'POST',
      body: JSON.stringify({
        decision,
        reason,
        ...(decision === 'approve' ? { valid_for_seconds: 3_600 } : {}),
      }),
    },
  )
}

export function applyCommercialAction(
  projectId: string,
  actionId: string,
  reason: string,
): Promise<CommercialActionRecord> {
  return request<CommercialActionRecord>(
    `/api/projects/${encodeURIComponent(projectId)}/commercial-actions/${encodeURIComponent(actionId)}/apply`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  )
}

export function cancelCommercialAction(
  projectId: string,
  actionId: string,
  reason: string,
): Promise<CommercialActionRecord> {
  return request<CommercialActionRecord>(
    `/api/projects/${encodeURIComponent(projectId)}/commercial-actions/${encodeURIComponent(actionId)}/cancel`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  )
}

export function upsertConnector(id: string, input: {
  type: string
  display_name: string
  scope?: string
  secret_env?: string | null
  endpoint?: string | null
  secret?: string
}): Promise<Connector> {
  return request<Connector>(`/api/connectors/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export function revokeConnector(id: string): Promise<Connector> {
  return request<Connector>(`/api/connectors/${encodeURIComponent(id)}/revoke`, { method: 'POST' })
}

export function listMarketplaceGuides(): Promise<MarketplaceGuide[]> {
  return request<MarketplaceGuide[]>('/api/connectors/marketplaces')
}

export function readMarketplace(id: string, input: MarketplaceReadInput): Promise<MarketplaceRead> {
  return request<MarketplaceRead>(`/api/connectors/${encodeURIComponent(id)}/marketplace-read`, {
    method: 'POST', body: JSON.stringify(input),
  })
}

export function getMarketplaceAccess(projectId: string): Promise<MarketplaceAccess> {
  return request<MarketplaceAccess>(`/api/projects/${encodeURIComponent(projectId)}/marketplace-access`)
}

const shopPath = (projectId: string, shopId?: string) => `/api/projects/${encodeURIComponent(projectId)}/shops${shopId ? `/${encodeURIComponent(shopId)}` : ''}`

export function listProjectShops(projectId: string): Promise<ProjectShop[]> {
  return request(shopPath(projectId))
}

export function saveProjectShop(projectId: string, input: ShopIdentityInput, previous?: ProjectShop): Promise<ProjectShop> {
  return request(shopPath(projectId, previous?.id), {
    method: previous ? 'PUT' : 'POST',
    body: JSON.stringify({ ...input, ...(previous ? { expected_revision: previous.revision } : {}) }),
  })
}

export function controlProjectShop(projectId: string, shop: ProjectShop, operation: 'open' | 'close' | 'revoke'): Promise<ProjectShop> {
  return request(`${shopPath(projectId, shop.id)}/${operation}`, {
    method: 'POST', body: JSON.stringify({ expected_revision: shop.revision }),
  })
}

export function grantProjectShop(projectId: string, shop: ProjectShop, days: number): Promise<ProjectShop> {
  return request(`${shopPath(projectId, shop.id)}/grant`, { method: 'POST', body: JSON.stringify({
    expected_revision: shop.revision, session_id: shop.session_id, confirmed_identity: true,
    expires_in_seconds: days * 86400,
  }) })
}

export function getProjectShopData(projectId: string, shopId: string): Promise<ShopData[]> {
  return request(`${shopPath(projectId, shopId)}/data`)
}

export function grantMarketplaceAccess(projectId: string, input: {
  expected_revision: number; connector_id: string; shop_id: string; expires_in_seconds: number
}): Promise<MarketplaceAccess> {
  return request<MarketplaceAccess>(`/api/projects/${encodeURIComponent(projectId)}/marketplace-access`, {
    method: 'POST', body: JSON.stringify(input),
  })
}

export function revokeMarketplaceAccess(projectId: string, bindingId: string, revision: number): Promise<MarketplaceAccess> {
  return request<MarketplaceAccess>(`/api/projects/${encodeURIComponent(projectId)}/marketplace-access/${encodeURIComponent(bindingId)}/revoke`, {
    method: 'POST', body: JSON.stringify({ expected_revision: revision }),
  })
}

export function getSkillRepositoryCapabilities(signal?: AbortSignal): Promise<SkillRepositoryCapabilities> {
  return request('/api/skills/repository', { signal, cache: 'no-store' })
}
export function listSkillImports(signal?: AbortSignal): Promise<SkillImportView[]> {
  return request('/api/skills/imports', { signal, cache: 'no-store' })
}
export function getSkillImport(identifier: string, signal?: AbortSignal): Promise<SkillImportView> {
  return request(`/api/skills/imports/${encodeURIComponent(identifier)}`, { signal, cache: 'no-store' })
}
export function reviewSkillImport(identifier: string, payload: SkillImportReviewRequest, signal?: AbortSignal): Promise<SkillImportView> {
  return request(`/api/skills/imports/${encodeURIComponent(identifier)}/review`, { method: 'POST', body: JSON.stringify(payload), signal, cache: 'no-store' })
}
export function getSkillImportFile(identifier: string, path: string, signal?: AbortSignal): Promise<SkillFilePreview> {
  return request(`/api/skills/imports/${encodeURIComponent(identifier)}/files?${new URLSearchParams({ path })}`, { signal, cache: 'no-store' })
}
export function skillImportDownloadUrl(identifier: string, path: string): string {
  return `${apiBaseUrl}/api/skills/imports/${encodeURIComponent(identifier)}/download?${new URLSearchParams({ path })}`
}
function skillPackagePath(name: string, hash: string): string {
  return `/api/skills/repository/${encodeURIComponent(name)}/versions/${encodeURIComponent(hash)}`
}
export function getSkillPackage(name: string, hash: string, signal?: AbortSignal): Promise<SkillPackageView> {
  return request(skillPackagePath(name, hash), { signal, cache: 'no-store' })
}
export function getSkillPackageFile(name: string, hash: string, path: string, signal?: AbortSignal): Promise<SkillFilePreview> {
  return request(`${skillPackagePath(name, hash)}/files?${new URLSearchParams({ path })}`, { signal, cache: 'no-store' })
}
export function skillPackageDownloadUrl(name: string, hash: string): string {
  return `${apiBaseUrl}${skillPackagePath(name, hash)}/download`
}
export function selectPrivateSkill(name: string, payload: SkillRepositorySelection, signal?: AbortSignal): Promise<Pick<SkillPackageView, 'skill' | 'current_target'>> {
  return request(`/api/skills/repository/${encodeURIComponent(name)}/selection`, { method: 'POST', body: JSON.stringify(payload), signal, cache: 'no-store' })
}
export function submitSkillImport(file: File, requestId: string, signal?: AbortSignal): Promise<SkillImportView> {
  return request(`/api/skills/install?${new URLSearchParams({ request_id: requestId, filename: file.name })}`, {
    method: 'POST',
    body: file,
    signal,
    cache: 'no-store',
    headers: { 'Content-Type': file.type || 'application/zip' },
  })
}

export function runEventUrl(runId: string): string {
  return apiUrl(`/api/runs/${runId}/events`)
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'The TradeMind backend returned an unknown error.'
}

export function listTaskExperiences(scheduleId: string): Promise<TaskExperienceLedger> {
  return request<TaskExperienceLedger>(`/api/schedules/${encodeURIComponent(scheduleId)}/experiences`)
}

export function retireTaskExperience(
  scheduleId: string,
  entryId: string,
  input: { expected_version: number; reason: string },
): Promise<TaskExperienceLedger> {
  return request<TaskExperienceLedger>(
    `/api/schedules/${encodeURIComponent(scheduleId)}/experiences/${encodeURIComponent(entryId)}/retire`,
    { method: 'POST', body: JSON.stringify(input) },
  )
}
