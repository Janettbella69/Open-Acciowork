import type { OpportunityProject, ProtocolMetadata, RunRecord } from './types'

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
): Promise<RunRecord> {
  return request<RunRecord>(`/api/projects/${projectId}/runs`, {
    method: 'POST',
    body: JSON.stringify({
      prompt,
      ...(resumeSessionId ? { resume_session_id: resumeSessionId } : {}),
    }),
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
