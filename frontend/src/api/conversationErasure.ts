export interface ConversationDeletionPreview {
  kind: 'conversation'
  project_id: string
  selected_run_id: string
  request_id: string
  reviewed_at: string
  fingerprint: string
  run_ids: string[]
  file_count: number
  source_bytes: number
  native_session_count: number
  memory_count: number
  retained: string[]
}

export interface ConversationDeletionResult {
  status: 'deleted'
  project_id: string
  request_id: string
  run_ids: string[]
}
