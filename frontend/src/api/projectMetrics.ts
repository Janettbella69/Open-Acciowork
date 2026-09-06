export type MetricDistribution = {
  observed_count: number
  unavailable_count: number
  p50: number | null
  p95: number | null
  observed_total: number | null
}

export type MetricRate = { numerator: number; denominator: number; value: number | null }

export type ProjectRunMetrics = {
  run_count: number
  status_counts: Record<string, number>
  execution_failure_rate: MetricRate
  duration_ms: MetricDistribution
  queue_duration_ms: MetricDistribution
  ttft_ms: MetricDistribution
  cost_estimate_usd: MetricDistribution | null
  cost_basis_counts: Record<string, number>
}

export type InvocationMetricsGroup = {
  identifier: string | null
  run_count: number
  invocations: {
    invocation_count: number
    completed_count: number
    failed_count: number
    unknown_count: number
    pending_count: number
    failure_rate: MetricRate
    duration_ms: MetricDistribution
    queue_duration_ms: MetricDistribution
  }
}

export type ProjectMetrics = {
  schema_version: '1'
  project_id: string
  window: {
    created_since: string
    created_until: string
    observed_at: string
    selection_basis: 'run.created_at'
  }
  coverage: {
    matching_runs: number
    selected_runs: number
    omitted_by_run_limit: number
    unavailable_event_replays: number
    unavailable_reasons: Record<string, number>
    invalid_created_at_runs: number
    event_bytes_read: number
    event_count_read: number
    unattributed_tool_events?: number
    unclassified_tool_invocations?: number
    group_overflow_associations?: number
  }
  summary: ProjectRunMetrics
  providers: Array<{
    dimensions: { provider_profile_id: string | null; configured_model: string | null }
    runs: ProjectRunMetrics
  }>
  models: Array<{ dimensions: { observed_model: string | null }; runs: ProjectRunMetrics }>
  skills: InvocationMetricsGroup[]
  mcp_tools: InvocationMetricsGroup[]
  errors: Array<{ code: string | null; failed_run_count: number }>
  business_outcome_available: false
}
