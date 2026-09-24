export type SessionMatch = {
  run_id: string
  status: string
  created_at: string
  source: 'prompt' | 'user_message' | 'assistant_message' | 'result'
  excerpt: string
  event_sequence: number | null
}

export type SessionSearch = {
  project_id: string
  query: string
  observed_at: string
  matches: SessionMatch[]
  total_runs: number
  scanned_runs: number
  skipped_event_logs: number
  truncated: boolean
}
