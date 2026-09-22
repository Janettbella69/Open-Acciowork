import type { RunMode, RunStatus, RunTelemetry } from './types'

export type RunResources = {
  schema_version: '1'
  run_id: string
  project_id: string
  trace_id: string
  mode: RunMode
  status: RunStatus
  observed_at: string
  computed_through_sequence: number
  observations_available: boolean
  unavailable_reason: string | null
  limits: {
    frozen: boolean
    sdk_budget_usd: number | null
    turn_limit: number | null
    wall_clock_deadline: string | null
  }
  elapsed_seconds: number | null
  remaining_seconds: number | null
  remaining_budget_usd: number | null
  cost: RunTelemetry['cost']
  tokens: RunTelemetry['tokens']
  research_units: Array<{ id: string; description: string }>
  agents: Array<{
    agent_id: string
    agent_type: string | null
    state: 'started' | 'stopped' | 'unknown'
    last_observed_at: string
  }>
  tasks: Array<{
    task_id: string
    task_type: string | null
    tool_use_id: string | null
    requested_agent_type: string | null
    description: string | null
    status: 'running' | 'completed' | 'failed' | 'stopped' | 'unknown'
    last_tool_name: string | null
    started_at: string | null
    finished_at: string | null
    last_observed_at: string
    usage: {
      total_tokens: number | null
      tool_uses: number | null
      duration_ms: number | null
      source_sequence: number | null
      availability: 'reported' | 'unavailable' | 'inconsistent'
    }
    warnings: string[]
  }>
}
