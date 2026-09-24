import type { RunStatus } from './types'

export type TaskRow = {
  run_id: string
  project_id: string
  project_name: string
  prompt: string
  status: RunStatus
  created_at: string
  pending_approvals: number
  max_budget_usd: number | null
  estimated_cost_usd: number | null
  result_preview: string | null
  observations_available: boolean
}

export type TaskOverview = {
  observed_at: string
  rows: TaskRow[]
  total_visible_runs: number
  truncated: boolean
}
