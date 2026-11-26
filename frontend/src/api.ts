// The backend API (backend/src/ophira_backend/main.py). Same origin: the dev server and the
// site proxy both forward /api to the backend.

export type Project = { id: string; name: string; created_at: string; session_id: string | null }
export type Run = {
  id: string
  project_id: string
  prompt: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  created_at: string
  finished_at: string | null
  error: string | null
}
export type ProjectDetail = Project & { runs: Run[]; active_run_id: string | null }
export type WorkspaceFile = { path: string; size: number; modified_at: string }

/** One line of a turn's log: an SDK message as the SDK sent it, or a host event. */
export type RunEvent = {
  seq: number
  at: string
  source: 'sdk' | 'host'
  type: string
  data: Record<string, unknown>
}

export type Decision = {
  allow: boolean
  message?: string
  answers?: Record<string, string | string[]>
  response?: string
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // not JSON; keep the status text
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  projects: () => call<Project[]>('/api/projects'),
  createProject: (name: string) => call<Project>('/api/projects', json({ name })),
  project: (id: string) => call<ProjectDetail>(`/api/projects/${id}`),
  startRun: (projectId: string, prompt: string) =>
    call<Run>(`/api/projects/${projectId}/runs`, json({ prompt })),
  cancel: (runId: string) => call<{ cancelled: boolean }>(`/api/runs/${runId}/cancel`, { method: 'POST' }),
  decide: (runId: string, requestId: string, decision: Decision) =>
    call<{ ok: boolean }>(`/api/runs/${runId}/permissions/${requestId}`, json(decision)),
  files: (projectId: string) => call<WorkspaceFile[]>(`/api/projects/${projectId}/files`),
  fileUrl: (projectId: string, path: string) =>
    `/api/projects/${projectId}/files/${path.split('/').map(encodeURIComponent).join('/')}`,
  upload: (projectId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return call<{ saved: string[] }>(`/api/projects/${projectId}/files`, { method: 'POST', body: form })
  },
  feedback: (projectId: string, text: string, runId?: string) =>
    call<{ at: string }>(`/api/projects/${projectId}/feedback`, json({ text, run_id: runId })),
}

/** Follow a turn's events; the server replays what was missed and closes when the turn ends. */
export function followRun(runId: string, onEvent: (event: RunEvent) => void, onEnd: () => void) {
  const source = new EventSource(`/api/runs/${runId}/events`)
  source.onmessage = (message) => {
    const event = JSON.parse(message.data) as RunEvent
    onEvent(event)
    if (event.type === 'run.finished') {
      source.close()
      onEnd()
    }
  }
  source.onerror = () => {
    // EventSource reconnects with Last-Event-ID; a closed stream after the end is normal.
    if (source.readyState === EventSource.CLOSED) onEnd()
  }
  return () => source.close()
}
