export const dimensions = ['demand', 'supply', 'capital', 'sales'] as const
export type Dimension = (typeof dimensions)[number]

export type ValidationStatus = 'unverified' | 'in_progress' | 'validated' | 'blocked' | 'rejected'
export type ProjectStage = 'discovery' | 'validation' | 'execution_ready' | 'pilot' | 'stopped'
export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_permission'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type Hypothesis = {
  id: string
  dimension: Dimension
  statement: string
  validation_criteria: string[]
  priority: number
  status: ValidationStatus
  evidence_ids: string[]
  created_at: string
}

export type Evidence = {
  id: string
  dimension: Dimension
  kind: 'observed_fact' | 'user_statement' | 'community_signal' | 'inference'
  claim: string
  source_title: string
  source_url: string | null
  confidence: number
  supports_hypothesis_ids: string[]
  recorded_by: string
  recorded_at: string
}

export type DimensionAssessment = {
  dimension: Dimension
  status: ValidationStatus
  score: number
  summary: string
  gaps: string[]
  evidence_ids: string[]
  updated_by: string
  updated_at: string
}

export type StageDecision = {
  id: string
  decision: 'advance' | 'hold' | 'stop'
  current_stage: ProjectStage
  target_stage: ProjectStage
  weakest_dimension: Dimension
  rationale: string
  next_actions: string[]
  next_agent: string | null
  made_by: string
  created_at: string
}

export type OpportunityProject = {
  id: string
  name: string
  objective: string
  product_brief: string
  target_markets: string[]
  constraints: string[]
  stage: ProjectStage
  hypotheses: Hypothesis[]
  evidence: Evidence[]
  assessments: Record<Dimension, DimensionAssessment>
  stage_decisions: StageDecision[]
  revision: number
  created_at: string
  updated_at: string
}

export type RunRecord = {
  id: string
  trace_id: string
  project_id: string
  prompt: string
  status: RunStatus
  sdk_session_id: string | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type EventActor = {
  type: string
  name: string
  agent_id: string | null
}

export type OphiraEvent = {
  id: string
  sequence: number
  type: string
  run_id: string
  project_id: string
  trace_id: string
  timestamp: string
  actor: EventActor
  parent_id: string | null
  tool_use_id: string | null
  data: Record<string, unknown>
}

export type ProtocolMetadata = {
  version: string
  event_types: string[]
  run_statuses: string[]
  dimensions: string[]
  project_stages: string[]
  permission_decisions: string[]
}

export type PendingPermission = {
  id: string
  toolName: string
  toolInput: unknown
  actor: EventActor
}
