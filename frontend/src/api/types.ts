export const dimensions = ['demand', 'supply', 'capital', 'sales'] as const
export const RESOURCE_EVENT = {
  ARTIFACT_REGISTERED: 'artifact.registered',
  ARTIFACT_UPDATED: 'artifact.updated',
  MEMORY_PROPOSED: 'memory.proposed',
} as const
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
export type RunMode = 'standard' | 'deep_research' | 'plan'
export type RunInterjectionMode = 'now' | 'next_turn' | 'interrupt'
export type RunInterjectionReceipt = {
  request_id: string
  run_id: string
  prompt: string
  mode: RunInterjectionMode
  status: 'queued' | 'dispatching' | 'injected' | 'uncertain' | 'blocked' | 'carried'
  next_run_id: string | null
}
export type RunRetryStrategy = 'resume' | 'new_branch'
export type ScheduleKind = 'task' | 'heartbeat'
export type AgentTemplate = 'general' | 'research' | 'sourcing' | 'marketing' | 'docs' | 'opportunity'
export type PermissionModePolicy = 'default' | 'auto'
export type MemoryScope = 'user' | 'project' | 'agent'
export type MemoryStatus = 'proposed' | 'approved' | 'rejected' | 'disabled'
export type MissedRunPolicy = 'catch_up' | 'skip'

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
  source_run_id?: string | null
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
  content_storage?: 'workspace' | 'snapshot'
  parent_artifact_id?: string | null
  created_by?: string | null
  edit_request_id?: string | null
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
  template_origin?: import('./projectTemplates').ProjectTemplateOrigin | null
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
  event_trigger_id?: string | null
  resume_session_id?: string | null
  manual_compaction?: boolean
  session_compaction_epoch?: number
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

export type TelemetryMetric<T> = {
  value: T | null
  availability: 'observed' | 'unavailable'
  unit: string
  source: string
  source_event_sequences: number[]
  scope: string | null
  unavailable_reason: string | null
}

export type ToolTiming = {
  tool_use_id: string
  tool_name: TelemetryMetric<string>
  lifecycle_status: 'planned' | 'running' | 'completed' | 'failed' | 'ambiguous'
  planned_event_sequences: number[]
  started_event_sequences: number[]
  terminal_event_sequences: number[]
  queue_duration_ms: TelemetryMetric<number>
  run_duration_ms: TelemetryMetric<number>
  quality_warnings: string[]
}

export type DurationAggregate = {
  observed_count: number
  unavailable_count: number
  observed_total_ms: TelemetryMetric<number>
  observed_max_ms: TelemetryMetric<number>
}

export type RunTelemetry = {
  schema_version: '1'
  run_id: string
  project_id: string
  trace_id: string
  computed_through_sequence: number
  event_log_complete_and_unambiguous: TelemetryMetric<boolean>
  prompt_prefix_hash: TelemetryMetric<string>
  latency: {
    run_queue_duration_ms: TelemetryMetric<number>
    total_task_duration_ms: TelemetryMetric<number>
    sdk_duration_ms: TelemetryMetric<number>
    sdk_api_duration_ms: TelemetryMetric<number>
    ttft_ms: TelemetryMetric<number>
    first_visible_text_ms: TelemetryMetric<number>
    first_progress_ms: TelemetryMetric<number>
    first_useful_typed_card_ms: TelemetryMetric<number>
    first_useful_typed_card_component: TelemetryMetric<string>
  }
  tools: {
    timings: ToolTiming[]
    unattributed_event_sequences: number[]
    queue_durations: DurationAggregate
    run_durations: DurationAggregate
  }
  tokens: {
    input_tokens: TelemetryMetric<number>
    output_tokens: TelemetryMetric<number>
    cache_read_input_tokens: TelemetryMetric<number>
    cache_creation_input_tokens: TelemetryMetric<number>
    cache_hit: TelemetryMetric<boolean>
  }
  cost: {
    estimated_cost_usd: TelemetryMetric<number>
    cost_basis: TelemetryMetric<string>
  }
  provider: {
    provider_profile_id: TelemetryMetric<string>
    provider_display_name: TelemetryMetric<string>
    configured_model: TelemetryMetric<string>
    observed_models: TelemetryMetric<string[]>
  }
  outcome: {
    run_status: RunStatus
    completed: TelemetryMetric<boolean>
    result_subtype: TelemetryMetric<string>
    result_is_error: TelemetryMetric<boolean>
    turn_count: TelemetryMetric<number>
  }
  quality_warnings: Array<{
    code: string
    detail: string
    event_sequences: number[]
  }>
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
  stream_end_event: string
  run_statuses: string[]
  run_modes: string[]
  provider_error_codes: string[]
  dimensions: string[]
  project_stages: string[]
  permission_decisions: string[]
  permission_modes: PermissionModePolicy[]
  schedule_kinds: ScheduleKind[]
  feedback_kinds: FeedbackKind[]
  agent_templates: AgentTemplate[]
  memory_scopes: MemoryScope[]
  memory_statuses: MemoryStatus[]
  missed_run_policies: MissedRunPolicy[]
  commercial_action_operations: CommercialActionOperation[]
  commercial_action_statuses: CommercialActionStatus[]
  commercial_review_decisions: CommercialReviewDecision[]
  external_execution_statuses: ExternalExecutionStatus[]
  presentation_frame_types: PresentationFrameType[]
  presentation_schema_versions: PresentationSchemaVersion[]
  presentation_components: PresentationComponent[]
}

export type RunStreamEnd = {
  run_id: string
  project_id: string
  last_sequence: number
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
  durable?: boolean
  state?: string
  toolUseId?: string | null
  expiresAt?: string | null
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

export type CommercialActionOperation =
  | 'create_resource'
  | 'update_resource'
  | 'delete_resource'
  | 'adjust_inventory'
  | 'change_price'
  | 'send_message'
  | 'publish_content'
  | 'commit_spend'

export type CommercialActionStatus =
  | 'staged'
  | 'approved'
  | 'rejected'
  | 'cancelled'
  | 'expired'
  | 'execution_requested'
  | 'applied'
  | 'execution_failed'
export type CommercialReviewDecision = 'approve' | 'reject'
export type ExternalExecutionStatus =
  | 'not_executed'
  | 'attempting'
  | 'retryable_failed'
  | 'succeeded'
  | 'terminal_failed'

export type CommercialActionRecord = {
  id: string
  project_id: string
  source_run_id: string
  source_session_id: string
  staged_by: string
  identity_binding: 'local_only' | 'authenticated_principal'
  idempotency_key: string | null
  source_receipt_id: string | null
  source_connector_config_hash: string | null
  operation: CommercialActionOperation
  target: {
    connector_id: string
    resource_type: string
    resource_key: string
    expected_version: string
  }
  observed_state: Record<string, unknown>
  changes: Array<{ field: string; before: unknown; after: unknown }>
  reason: string
  project_revision_at_stage: number
  staged_policy_updated_at: string | null
  status: CommercialActionStatus
  review: null | {
    decision: CommercialReviewDecision
    actor_id: string
    reason: string
    decided_at: string
    valid_until: string | null
    policy_updated_at: string | null
  }
  execution_request: null | {
    id: string
    external_status: ExternalExecutionStatus
    adapter_required: true
    adapter_type: string | null
    attempt_count: number
    retry_not_before: string | null
    last_error_code: string | null
    last_error_message: string | null
    receipt: null | {
      id: string
      remote_action_id: string
      remote_version: string | null
      http_status: number
      response_hash: string
      adapter_attempts: number
      issued_at: string
    }
  }
  created_at: string
  expires_at: string
  updated_at: string
}

export type PresentationFrameType = 'partial' | 'final' | 'discarded'
export const UI_PRESENTATION_EVENT = 'ui.presentation' as const
export const PRESENTATION_SCHEMA_VERSION = '1' as const
export const PRESENTATION_COMPONENT = {
  EVIDENCE_COMPARISON: 'evidence_comparison',
  SUPPLIER_COMPARISON: 'supplier_comparison',
  UNIT_ECONOMICS: 'unit_economics',
  DECISION_BRIEF: 'decision_brief',
  COMMERCIAL_ACTION_DIFF: 'commercial_action_diff',
  ARTIFACT_LIST: 'artifact_list',
} as const
export const presentationSchemaVersions = [PRESENTATION_SCHEMA_VERSION] as const
export const presentationComponents = Object.values(PRESENTATION_COMPONENT)
export type PresentationSchemaVersion = (typeof presentationSchemaVersions)[number]
export type PresentationComponent = (typeof presentationComponents)[number]

export type CommercialNumericLimit = {
  field: string
  minimum: number | null
  maximum: number | null
}

export type OrgPolicy = {
  permission_mode: 'default' | 'auto'
  allowed_provider_ids: string[]
  allowed_skill_ids: string[]
  remote_mcp_enabled: boolean
  allowed_mcp_hosts: string[]
  browser_enabled: boolean
  computer_control_enabled: boolean
  allowed_desktop_bundle_ids: string[]
  channel_delivery_enabled: boolean
  domain_allowlist: string[]
  commercial_actions_enabled: boolean
  allowed_commercial_operations: CommercialActionOperation[]
  allowed_commercial_connector_ids: string[]
  commercial_max_changes_per_action: number
  commercial_forbidden_fields: string[]
  commercial_numeric_limits: CommercialNumericLimit[]
  updated_at: string
}

export type DesktopStatus = {
  platform: 'macos'
  screen_recording_authorized: boolean
  accessibility_authorized: boolean
}

export type UserMcpTool = { name: string; policy: 'allow' | 'ask' | 'deny' }
export type UserMcpConnector = {
  id: string
  project_id: string
  name: string
  url: string
  revision: number
  enabled: boolean
  reviewed_at: string | null
  review_error: string | null
  auth_mode: 'bearer' | 'oauth'
  oauth_issuer: string | null
  oauth_client_configured: boolean
  oauth_client_auth_method: 'none' | 'client_secret_basic' | 'client_secret_post' | null
  credential_configured: boolean
  tools: UserMcpTool[]
}
export type UserMcpCatalogItem = {
  id: string
  name: string
  url: string
  description: string
}
export type UserMcpOAuthSetup = {
  redirect_uri: string | null
  client_metadata_url: string | null
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

export type TaskExperienceRevision = {
  key: string
  category: 'selector_failure' | 'threshold_observation' | 'retrieval_lesson'
  observation: string
  lesson: string
  version: number
  source_run_id: string
  source_trace_id: string
  source_session_id: string
  source_event_ids: string[]
  source_event_hashes: Record<string, string>
  write_event_id: string
  config_hash: string
  content_hash: string
  created_at: string
  origin: 'agent_observation'
  trust: 'untrusted_task_data'
}

export type TaskExperienceEntry = {
  id: string
  key: string
  revisions: TaskExperienceRevision[]
  pruned_versions: number
  stale: boolean
  retired_at: string | null
  retired_by: string | null
  retirement_reason: string | null
}

export type TaskExperienceLedger = {
  schema_version: 1
  project_id: string
  schedule_id: string
  revision: number
  entries: TaskExperienceEntry[]
  current_config_hash: string
  evicted_retired_entries: number
  limits: {
    entries: number
    versions_per_entry: number
    writes_per_run: number
    context_chars: number
  }
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
  durable?: boolean
  state?: string
  tool_use_id?: string | null
  expires_at?: string | null
}

export type FeedbackKind = 'conclusion_wrong' | 'evidence_issue' | 'run_failure'
export type FeedbackStatus = 'open' | 'reviewed' | 'folded_into_skill' | 'rejected'

export type FeedbackCreate = {
  project_id?: string | null
  run_id?: string | null
  message_id?: string | null
  evidence_id?: string | null
  kind: FeedbackKind
  user_note: string
}

export type FeedbackReview = {
  expected_revision: number
  status: FeedbackStatus
  note: string
  skill_id?: string
  skill_content_hash?: string
  regression_report?: string
  manual_skill_update_confirmed?: boolean
  l2_regression_passed_confirmed?: boolean
}

export type FeedbackReviewRecord = {
  from_status: FeedbackStatus
  to_status: FeedbackStatus
  note: string
  reviewed_at: string
  reviewer_tenant_id: string | null
  reviewer_principal_id: string | null
  reviewer_role: string | null
  skill_id: string | null
  skill_content_hash: string | null
  regression_report: string | null
  manual_skill_update_confirmed: boolean
  l2_regression_passed_confirmed: boolean
}

export type FeedbackItem = {
  id: string
  project_id: string | null
  run_id: string | null
  evidence_id: string | null
  message_id?: string | null
  context?: {
    trace_id: string
    run_status: RunStatus
    event_id: string | null
    event_type: string | null
    message_text: string | null
    evidence: Evidence | null
    captured_at: string
  } | null
  source?: 'user' | 'system'
  candidate_state?: 'unlabeled'
  kind: FeedbackKind
  user_note: string
  status: FeedbackStatus
  revision?: number
  reviews?: FeedbackReviewRecord[]
  created_at: string
}

export type PermissionDecision = 'allow_once' | 'allow_run' | 'allow_persistent' | 'deny'
export type PermissionAnswers = Record<string, string | string[]>
