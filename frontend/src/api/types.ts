export const dimensions = ['demand', 'supply', 'capital', 'sales'] as const
export type Dimension = (typeof dimensions)[number]

export type ValidationStatus = 'unverified' | 'in_progress' | 'validated' | 'blocked' | 'rejected'
export type ProjectStage = 'discovery' | 'validation' | 'execution_ready' | 'pilot' | 'stopped'
export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_permission'
  | 'paused'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'
export type RunMode = 'standard' | 'deep_research'
export type RunRetryStrategy = 'resume' | 'new_branch'
export type ScheduleKind = 'task' | 'heartbeat'
export type AgentTemplate = 'general' | 'research' | 'sourcing' | 'marketing' | 'docs' | 'opportunity'

export type Hypothesis = {
  id: string
  dimension: Dimension | null
  tags: string[]
  statement: string
  validation_criteria: string[]
  priority: number
  status: ValidationStatus
  evidence_ids: string[]
  created_at: string
}

export type Evidence = {
  id: string
  dimension: Dimension | null
  tags: string[]
  kind: 'observed_fact' | 'user_statement' | 'community_signal' | 'inference'
  claim: string
  source_title: string
  source_url: string | null
  source_type: 'web' | 'file' | 'user' | 'tool' | 'calculation'
  source_path: string | null
  retrieved_at: string
  observed_at: string | null
  excerpt: string | null
  location: string | null
  freshness_note: string | null
  status: 'active' | 'stale' | 'removed'
  confidence: number
  supports_hypothesis_ids: string[]
  recorded_by: string
  recorded_at: string
}

export type Decision = {
  id: string
  question: string
  outcome: string
  decision_type: 'go' | 'test' | 'no_go' | 'ranking' | 'plan' | 'other'
  rationale: string
  evidence_ids: string[]
  hypothesis_ids: string[]
  assumptions: string[]
  conditions: string[]
  next_actions: string[]
  status: 'active' | 'needs_review' | 'superseded'
  made_by: string
  review_at: string | null
  invalidated_by_evidence_ids: string[]
  created_at: string
}

export type Artifact = {
  id: string
  project_id: string
  path: string
  name: string
  mime: string
  version: number
  run_id: string | null
  source_ids: string[]
  checksum: string
  size_bytes: number
  download_url: string
  created_at: string
  updated_at: string
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
  assessments: Partial<Record<Dimension, DimensionAssessment>>
  stage_decisions: StageDecision[]
  decisions: Decision[]
  artifacts: Artifact[]
  revision: number
  created_at: string
  updated_at: string
}

export type RunRecord = {
  id: string
  trace_id: string
  project_id: string
  prompt: string
  agent_id: string | null
  schedule_id?: string | null
  resume_session_id?: string | null
  mode: RunMode
  provider_profile_id: string | null
  provider_snapshot: Record<string, unknown> | null
  skill_snapshot: Array<Record<string, unknown>>
  status: RunStatus
  sdk_session_id: string | null
  error: string | null
  error_code: string | null
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
  sdk_session_id: string | null
  provider_profile_id: string | null
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
  run_modes: string[]
  provider_error_codes: string[]
  dimensions: string[]
  project_stages: string[]
  permission_decisions: string[]
  agent_templates: AgentTemplate[]
}

export type AgentProfile = {
  id: string
  name: string
  template: AgentTemplate
  role_description: string
  model: string | null
  revision: number
  created_at: string
  updated_at: string
}

export type ProviderCapabilities = {
  tool_use: boolean
  streaming: boolean
  images: boolean
  prompt_caching: boolean
  extended_thinking: boolean
  tool_search: boolean
  native_web_search?: boolean
  native_web_fetch?: boolean
  context_window_tokens?: number | null
}

export type ProviderProfile = {
  id: string
  display_name: string
  base_url: string | null
  auth_env_name: 'ANTHROPIC_API_KEY' | 'ANTHROPIC_AUTH_TOKEN'
  model: string
  default_haiku_model?: string | null
  default_sonnet_model?: string | null
  default_opus_model?: string | null
  subagent_model?: string | null
  budget_scale?: number
  terms_checked_at?: string | null
  region: string
  production_eligible: boolean
  enabled: boolean
  secret_configured: boolean
  last_verified_at: string | null
  last_error_code: string | null
  last_error_message: string | null
  capabilities: ProviderCapabilities
}

export type ProviderPreflightResult = {
  provider_profile_id: string
  ok: boolean
  error_code: string | null
  message: string
  sdk_session_id: string | null
  model: string | null
  mcp_connected: boolean
  discovered_skills: string[]
  checked_at: string
}

export type EvidenceStatus = 'active' | 'stale' | 'removed'

export type EvidenceStatusChange = {
  evidence: Evidence
  affected_decision_ids: string[]
}

export type SkillCheck = {
  name: string
  status: 'passed' | 'warning' | 'failed'
  detail: string
}

export type SkillTestResult = {
  skill_id: string
  ok: boolean
  checks: SkillCheck[]
  checked_at: string
}

export type Connector = {
  id: string
  type: string
  display_name: string
  scope: string
  secret_env: string | null
  endpoint: string | null
  enabled: boolean
  health: 'unknown' | 'ok' | 'no_credential' | 'unreachable'
  last_checked_at: string | null
  created_at: string
}

export type SkillVersion = {
  id: string
  namespace: string
  name: string
  description: string
  version: string
  content_hash: string
  source: 'application' | 'tenant'
  enabled: boolean
  path: string
}

export type SchedulePermissionRule = {
  tool_name: string
  scope: string
}

export type Schedule = {
  id: string
  project_id: string
  provider_profile_id: string | null
  mode: RunMode
  kind: ScheduleKind
  prompt: string
  cron: string | null
  run_once_at: string | null
  timezone: string
  permission_ceiling: SchedulePermissionRule[]
  enabled: boolean
  next_fire_at: string | null
  last_fire_at: string | null
  last_run_id: string | null
  created_at: string
}

export type MemoryKind = 'preference' | 'fact' | 'decision' | 'lesson'
export type MemorySensitivity = 'normal' | 'sensitive'

export type MemoryItem = {
  id: string
  scope: 'user' | 'project' | 'agent'
  scope_id: string
  text: string
  kind: MemoryKind
  sensitivity: MemorySensitivity
  status: 'proposed' | 'approved' | 'rejected' | 'disabled'
  version: number
  created_at: string
  updated_at: string
}

export type PendingPermission = {
  id: string
  toolName: string
  toolInput: unknown
  actor: EventActor
  destructive: boolean
  persistable: boolean
  scope: string
  timeoutSeconds?: number
}

export type AskUserQuestionOption = {
  label: string
  description?: string
}

export type AskUserQuestionItem = {
  question: string
  header?: string
  options: AskUserQuestionOption[]
  multiSelect?: boolean
}

export type Notification = {
  id: string
  project_id: string | null
  run_id: string | null
  schedule_id: string | null
  intent_id: string | null
  severity: 'info' | 'warning' | 'error'
  title: string
  body: string
  read: boolean
  created_at: string
}

export type StandingIntent = {
  id: string
  project_id: string
  intent: string
  cooldown_hours: number
  max_triggers: number
  expires_at: string
  triggered_count: number
  last_triggered_at: string | null
  enabled: boolean
  created_at: string
}

export type OrgPolicy = {
  permission_mode: 'default' | 'auto'
  allowed_provider_ids: string[]
  allowed_skill_ids: string[]
  browser_enabled: boolean
  computer_control_enabled: boolean
  channel_delivery_enabled: boolean
  domain_allowlist: string[]
  updated_at: string
}

export type PermissionGrant = {
  id: string
  project_id: string | null
  tool_name: string
  path_prefix: string
  decision: string
  expires_at: string
  created_at: string
  revoked: boolean
}

export type PendingPermissionView = {
  id: string
  run_id: string
  project_id: string | null
  tool_name: string
  tool_input: Record<string, unknown>
  agent_id: string | null
  timeout_seconds: number
  created_at: string
  destructive: boolean
  persistable: boolean
  scope: string
}

export type FeedbackKind = 'conclusion_wrong' | 'evidence_issue' | 'run_failure'

export type FeedbackItem = {
  id: string
  project_id: string | null
  run_id: string | null
  evidence_id: string | null
  kind: FeedbackKind
  user_note: string
  status: string
  created_at: string
}

export type PermissionDecision = 'allow_once' | 'allow_run' | 'allow_persistent' | 'deny'
export type PermissionAnswers = Record<string, string | string[]>
