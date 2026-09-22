export type Json = Record<string, unknown>
export interface Source {
  path: string; sha256: string | null; status: string; error?: string; sequence_warning?: string
}
export interface Review {
  status: string; open_codes: string[]; notes: string; revision: number
  readonly reviewed_at?: string; readonly annotator?: string
}
export interface GoldItem {
  item_id: string; run_id: string; judge: string; item: string; source: string; split: string; batch: string
  annotation: { label: boolean; rationale: string; labeled_at: string } | null
  revision: number; trace_ids?: string[]
}
export interface Judge { question: string; positive: string; negative: string; positive_is_failure: boolean }
export interface Trace {
  id: string; experiment_id: string; experiment_name: string; case_key: string; run_id: string
  trace_id: string | null; execution_status: string | null; duration_ms: number | null
  cost_usd: number | null; cost_basis: string | null; snapshot_accepted: boolean | null
  legacy_passed: boolean | null; quality_status: string; event_count: number
  versions: Json; item_count: number; labeled_count: number; review: Review; issues: (Source & { file: string })[]
}
export interface TraceDetail extends Trace {
  prompt: string | null; final_text: string | null; final_text_source: string | null
  evidence_source: string | null; evidence: Json[]; artifacts: Json[]; snapshot: Json
  snapshot_eval: { findings?: { code: string; path?: string; message?: string }[] }
  summary: Json; result: Json; run: Json; sources: Record<string, Source>; items: GoldItem[]
  event_types: Record<string, number>; skill_snapshot: unknown; usage: unknown
}
export interface Experiment {
  id: string; name: string; provider: string | null; generated_at: string | null; case_count: number
  source: Source; counts: { completed: number; snapshot_accepted: number; snapshot_rejected: number
    snapshot_unknown: number; items: number; labeled: number }
}
export interface Event { id: string; sequence: number; type: string; timestamp: string; tool_use_id?: string; data: Json }
export interface EventPage { total: number; offset: number; events: Event[] }
export interface Comparison { cases: {
  case_key: string; left: Trace[]; right: Trace[]; comparable: boolean; change: string
  versions: { field: string; left: unknown; right: unknown; state: string }[]
}[] }

const base = '/api/acciowork'
export const fileUrl = (sha: string) => `${base}/files/${encodeURIComponent(sha)}`
export const exportUrl = (judge: string) => `${base}/labels/${encodeURIComponent(judge)}/export`
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(base + path, body === undefined ? undefined : {
    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Eval-Review': 'owner-ui' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(typeof error?.detail === 'string' ? error.detail : `请求失败 (${response.status})`)
  }
  return response.json()
}
export const api = {
  experiments: () => request<Experiment[]>('/experiments'),
  traces: (id = '') => request<Trace[]>(`/traces${id ? `?experiment_id=${encodeURIComponent(id)}` : ''}`),
  trace: (id: string) => request<TraceDetail>(`/traces/${id}`),
  events: (id: string, params: URLSearchParams) => request<EventPage>(`/traces/${id}/events?${params}`),
  queue: () => request<{ judges: Record<string, Judge>; items: GoldItem[] }>('/review'),
  label: (id: string, body: unknown) => request(`/items/${id}/label`, body),
  review: (id: string, body: Review) => request<Review>(`/traces/${id}/review`, {
    status: body.status, open_codes: body.open_codes, notes: body.notes, revision: body.revision,
  }),
  compare: (left: string, right: string) => request<Comparison>(`/compare?${new URLSearchParams({ left, right })}`),
}
