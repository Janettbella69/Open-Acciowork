import type {
  Artifact,
  Connector,
  Evidence,
  EvidenceStatus,
  EvidenceStatusChange,
  MemoryItem,
  OpportunityProject,
  ProtocolMetadata,
  ProviderPreflightResult,
  ProviderProfile,
  RunMode,
  RunRecord,
  Schedule,
  SkillTestResult,
  SkillVersion,
} from './types'

const configuredBaseUrl = import.meta.env.VITE_OPHIRA_API_URL?.trim() ?? ''
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '')

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
      const body = (await response.json()) as { detail?: string }
      message = body.detail ?? message
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
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

export function getProject(projectId: string): Promise<OpportunityProject> {
  return request<OpportunityProject>(`/api/projects/${projectId}`)
}

export function listProjectRuns(projectId: string): Promise<RunRecord[]> {
  return request<RunRecord[]>(`/api/projects/${projectId}/runs`)
}

export function startRun(
  projectId: string,
  prompt: string,
  resumeSessionId?: string | null,
  mode: RunMode = 'standard',
  providerProfileId?: string | null,
): Promise<RunRecord> {
  return request<RunRecord>(`/api/projects/${projectId}/runs`, {
    method: 'POST',
    body: JSON.stringify({
      prompt,
      mode,
      ...(resumeSessionId ? { resume_session_id: resumeSessionId } : {}),
      ...(providerProfileId ? { provider_profile_id: providerProfileId } : {}),
    }),
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

export function testSkill(skillId: string): Promise<SkillTestResult> {
  return request<SkillTestResult>(`/api/skills/${encodeURIComponent(skillId)}/test`, {
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

export function setSkillEnabled(skillId: string, enabled: boolean): Promise<SkillVersion> {
  return request<SkillVersion>(
    `/api/skills/${encodeURIComponent(skillId)}/enable?enabled=${enabled}`,
    { method: 'POST' },
  )
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
  provider_profile_id?: string | null
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

export function listMemory(): Promise<MemoryItem[]> {
  return request<MemoryItem[]>('/api/memory')
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

export function getRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${runId}`)
}

export function cancelRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${runId}/cancel`, { method: 'POST' })
}

export function resolvePermission(
  runId: string,
  requestId: string,
  decision: 'allow_once' | 'allow_run' | 'deny',
): Promise<void> {
  return request<void>(`/api/runs/${runId}/permissions/${requestId}`, {
    method: 'POST',
    body: JSON.stringify({ decision, reason: decision === 'deny' ? 'Denied by the user.' : '' }),
  })
}

export function runEventUrl(runId: string): string {
  return apiUrl(`/api/runs/${runId}/events`)
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'The OPHIRA backend returned an unknown error.'
}
