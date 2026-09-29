from enum import StrEnum


class Dimension(StrEnum):
    DEMAND = "demand"
    SUPPLY = "supply"
    CAPITAL = "capital"
    SALES = "sales"


class EvidenceKind(StrEnum):
    OBSERVED_FACT = "observed_fact"
    USER_STATEMENT = "user_statement"
    COMMUNITY_SIGNAL = "community_signal"
    INFERENCE = "inference"


class EvidenceSourceType(StrEnum):
    WEB = "web"
    FILE = "file"
    USER = "user"
    TOOL = "tool"
    CALCULATION = "calculation"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    REMOVED = "removed"


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    SUPERSEDED = "superseded"


class ValidationStatus(StrEnum):
    UNVERIFIED = "unverified"
    IN_PROGRESS = "in_progress"
    VALIDATED = "validated"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class ProjectStage(StrEnum):
    DISCOVERY = "discovery"
    VALIDATION = "validation"
    EXECUTION_READY = "execution_ready"
    PILOT = "pilot"
    STOPPED = "stopped"


class StageDecisionValue(StrEnum):
    ADVANCE = "advance"
    HOLD = "hold"
    STOP = "stop"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunMode(StrEnum):
    STANDARD = "standard"
    DEEP_RESEARCH = "deep_research"
    PLAN = "plan"


class RunInterjectionMode(StrEnum):
    NOW = "now"
    NEXT_TURN = "next_turn"
    INTERRUPT = "interrupt"


RESEARCH_UNITS = {
    "source-mapper": "覆盖独立原始来源，建立带日期和定位信息的来源地图。",
    "multilingual-researcher": "在任务指定的语言与地区中研究，并保留原文术语与译意。",
    "conflict-verifier": "独立核验相互冲突的说法，说明各来源口径、时效与局限。",
}


class RunRetryStrategy(StrEnum):
    RESUME = "resume"
    NEW_BRANCH = "new_branch"


class ProviderErrorCode(StrEnum):
    AUTH_REVOKED_OR_INVALID = "AUTH_REVOKED_OR_INVALID"
    BASE_URL_UNREACHABLE = "BASE_URL_UNREACHABLE"
    MODEL_NOT_AVAILABLE = "MODEL_NOT_AVAILABLE"
    QUOTA_OR_BILLING = "QUOTA_OR_BILLING"
    ANTHROPIC_PROTOCOL_INCOMPATIBLE = "ANTHROPIC_PROTOCOL_INCOMPATIBLE"
    MCP_INIT_FAILED = "MCP_INIT_FAILED"
    SKILL_DISCOVERY_FAILED = "SKILL_DISCOVERY_FAILED"
    SDK_RUNTIME_FAILED = "SDK_RUNTIME_FAILED"
    SDK_RESOURCE_LIMIT_EXCEEDED = "SDK_RESOURCE_LIMIT_EXCEEDED"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

# Transport watermark only: never written into the durable event sequence.
STREAM_END_EVENT = "stream.completed"


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_STATUS = "run.status"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERJECTION_REQUESTED = "run.interjection.requested"
    RUN_INTERJECTION_DISPATCHING = "run.interjection.dispatching"
    RUN_INTERJECTION_INJECTED = "run.interjection.injected"
    RUN_INTERJECTION_UNCERTAIN = "run.interjection.uncertain"
    RUN_INTERJECTION_BLOCKED = "run.interjection.blocked"
    RUN_INTERJECTION_CARRIED = "run.interjection.carried"
    CONTEXT_USAGE = "context.usage"
    CONTEXT_COMPACTED = "context.compacted"

    ASSISTANT_MESSAGE_STARTED = "assistant.message.started"
    ASSISTANT_TEXT_DELTA = "assistant.text.delta"
    ASSISTANT_MESSAGE_COMPLETED = "assistant.message.completed"
    ASSISTANT_REASONING_SUMMARY_DELTA = "assistant.reasoning_summary.delta"
    ASSISTANT_REASONING_SUMMARY_COMPLETED = "assistant.reasoning_summary.completed"

    TOOL_PLANNED = "tool.planned"
    TOOL_INPUT_DELTA = "tool.input.delta"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"
    PERMISSION_TIMED_OUT = "permission.timed_out"

    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_STOPPED = "subagent.stopped"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"

    SDK_SYSTEM = "sdk.system"
    SDK_RATE_LIMIT = "sdk.rate_limit"
    RUN_RESULT = "run.result"

    ARTIFACT_REGISTERED = "artifact.registered"
    ARTIFACT_UPDATED = "artifact.updated"
    MEMORY_PROPOSED = "memory.proposed"
    EVIDENCE_ADDED = "evidence.added"
    DECISION_RECORDED = "decision.recorded"
    UI_PRESENTATION = "ui.presentation"
    PROVIDER_PREFLIGHT = "provider.preflight"
    PROVIDER_PREFLIGHT_STARTED = "provider.preflight.started"
    PROVIDER_PREFLIGHT_PASSED = "provider.preflight.passed"
    PROVIDER_PREFLIGHT_FAILED = "provider.preflight.failed"

    PROJECT_CHANGED = "project.changed"

    SCHEDULE_FIRED = "schedule.fired"
    SCHEDULE_SKIPPED = "schedule.skipped"
    SCHEDULE_COMPLETED = "schedule.completed"
    SCHEDULE_FAILED = "schedule.failed"
    SCHEDULE_CANCELLED = "schedule.cancelled"


class PermissionDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_RUN = "allow_run"
    ALLOW_PERSISTENT = "allow_persistent"
    DENY = "deny"


class PermissionModePolicy(StrEnum):
    DEFAULT = "default"
    AUTO = "auto"


class MemoryOrigin(StrEnum):
    OWNER = "owner"
    AGENT = "agent"
    UNTRUSTED = "untrusted"
    SYSTEM = "system"


class ScheduleKind(StrEnum):
    TASK = "task"
    HEARTBEAT = "heartbeat"


class PrincipalRole(StrEnum):
    """Roles asserted by trusted ingress and constrained by project membership."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class StandingIntentWatcherStatus(StrEnum):
    """Durable lifecycle of a user-authorized standing-intent watcher."""

    IDLE = "idle"
    CHECKING = "checking"
    BACKOFF = "backoff"
    PAUSED = "paused"
    EXPIRED = "expired"
    TRIGGER_EXHAUSTED = "trigger_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNAUTHORIZED = "unauthorized"


class FeedbackKind(StrEnum):
    CONCLUSION_WRONG = "conclusion_wrong"
    EVIDENCE_ISSUE = "evidence_issue"
    RUN_FAILURE = "run_failure"


class AgentTemplate(StrEnum):
    GENERAL = "general"
    RESEARCH = "research"
    SOURCING = "sourcing"
    MARKETING = "marketing"
    DOCS = "docs"
    OPPORTUNITY = "opportunity"


class MemoryScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    AGENT = "agent"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    LESSON = "lesson"


class MemorySource(StrEnum):
    USER = "user"
    AGENT_PROPOSAL = "agent_proposal"
    DISTILLATION = "distillation"


class MemorySensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


class MissedRunPolicy(StrEnum):
    CATCH_UP = "catch_up"
    SKIP = "skip"


class CommercialActionOperation(StrEnum):
    """High-consequence commerce mutations that must use the staged-action gate."""

    CREATE_RESOURCE = "create_resource"
    UPDATE_RESOURCE = "update_resource"
    DELETE_RESOURCE = "delete_resource"
    ADJUST_INVENTORY = "adjust_inventory"
    CHANGE_PRICE = "change_price"
    SEND_MESSAGE = "send_message"
    PUBLISH_CONTENT = "publish_content"
    COMMIT_SPEND = "commit_spend"


class CommercialActionStatus(StrEnum):
    STAGED = "staged"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    EXECUTION_REQUESTED = "execution_requested"
    APPLIED = "applied"
    EXECUTION_FAILED = "execution_failed"


class CommercialReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ExternalExecutionStatus(StrEnum):
    """Durable state of the external side effect behind a reviewed action."""

    NOT_EXECUTED = "not_executed"
    ATTEMPTING = "attempting"
    RETRYABLE_FAILED = "retryable_failed"
    SUCCEEDED = "succeeded"
    TERMINAL_FAILED = "terminal_failed"


class PresentationFrameType(StrEnum):
    """Lifecycle of one server-owned typed presentation slot."""

    PARTIAL = "partial"
    FINAL = "final"
    DISCARDED = "discarded"


PRESENTATION_FRAME_PROTOCOL_VERSION = "1"


COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE = "https_json_action"


LEDGER_MCP_SERVER_NAME = "ophira_ledger"
SUPPLIER_MCP_SERVER_NAME = "ophira_supplier"
CUSTOMER_MCP_SERVER_NAME = "ophira_customer"
COMMERCE_MATH_MCP_SERVER_NAME = "ophira_commerce_math"
WEB_MCP_SERVER_NAME = "ophira_web"
FX_MCP_SERVER_NAME = "ophira_fx"
AUTOMATION_MCP_SERVER_NAME = "ophira_automation"
COMPLIANCE_MCP_SERVER_NAME = "ophira_compliance"
BROWSER_MCP_SERVER_NAME = "ophira_browser"
DESKTOP_MCP_SERVER_NAME = "ophira_desktop"
MEDIA_MCP_SERVER_NAME = "ophira_media"
PRESENTATION_MCP_SERVER_NAME = "ophira_presentation"
COMMERCIAL_ACTION_MCP_SERVER_NAME = "ophira_commercial_actions"
HEARTBEAT_OK = "HEARTBEAT_OK"
PERSISTENT_GRANT_DAYS = 30
APPROVED_MEMORY_CAP = 32
APPROVED_MEMORY_CHARS = 8_000

TOOL_GET_PROJECT_CONTEXT = "mcp__ophira_ledger__read_project_context"
TOOL_SEARCH_EVIDENCE = "mcp__ophira_ledger__search_evidence"
TOOL_SEARCH_PROJECT_KNOWLEDGE = "mcp__ophira_ledger__search_project_knowledge"
TOOL_READ_SOURCE_RECEIPT = "mcp__ophira_ledger__read_source_receipt"
TOOL_READ_MARKETPLACE = "mcp__ophira_ledger__read_marketplace"
TOOL_READ_GMAIL = "mcp__ophira_ledger__read_gmail"
TOOL_READ_OUTLOOK = "mcp__ophira_ledger__read_outlook"
TOOL_RECORD_HYPOTHESIS = "mcp__ophira_ledger__record_hypothesis"
TOOL_RECORD_EVIDENCE = "mcp__ophira_ledger__record_evidence"
TOOL_RECORD_EVIDENCE_BATCH = "mcp__ophira_ledger__record_evidence_batch"
TOOL_RECORD_DECISION = "mcp__ophira_ledger__record_decision"
TOOL_REGISTER_ARTIFACT = "mcp__ophira_ledger__register_artifact"
TOOL_UPDATE_EVIDENCE_STATUS = "mcp__ophira_ledger__update_evidence_status"
TOOL_PROPOSE_MEMORY = "mcp__ophira_ledger__propose_memory"
TOOL_SEARCH_MEMORY = "mcp__ophira_ledger__search_memory"
TOOL_SEARCH_PROJECT_SESSIONS = "mcp__ophira_ledger__search_project_sessions"

TOOL_SEARCH_PUBLIC_WEB = "mcp__ophira_web__search_public_web"
TOOL_FETCH_PAGE_SNAPSHOT = "mcp__ophira_web__fetch_page_snapshot"
TOOL_GET_ECB_FX_REFERENCE = "mcp__ophira_fx__get_ecb_fx_reference"

TOOL_COMPARE_SUPPLIER_QUOTES = "mcp__ophira_supplier__compare_supplier_quotes"
TOOL_SEARCH_SUPPLIERS = "mcp__ophira_supplier__search_suppliers"
TOOL_DRAFT_OUTBOUND = "mcp__ophira_supplier__draft_outbound"
TOOL_RANK_CUSTOMER_CANDIDATES = "mcp__ophira_customer__rank_customer_candidates"
TOOL_RECORD_INTERACTION = "mcp__ophira_customer__record_interaction"
TOOL_CALCULATE_LANDED_COST = "mcp__ophira_commerce_math__calculate_landed_cost"
TOOL_CALCULATE_UNIT_ECONOMICS = "mcp__ophira_commerce_math__calculate_unit_economics"
TOOL_CASHFLOW_PEAK = "mcp__ophira_commerce_math__cashflow_peak"
TOOL_SENSITIVITY_TABLE = "mcp__ophira_commerce_math__sensitivity_table"
TOOL_LOOKUP_REQUIREMENTS = "mcp__ophira_compliance__lookup_requirements"
TOOL_SNAPSHOT_PAGE = "mcp__ophira_browser__snapshot_page"
TOOL_ACT_ON_PAGE = "mcp__ophira_browser__act_on_page"
TOOL_SELECT_DESKTOP_WINDOW = "mcp__ophira_desktop__select_window"
TOOL_SNAPSHOT_DESKTOP = "mcp__ophira_desktop__snapshot_window"
TOOL_PRESS_DESKTOP = "mcp__ophira_desktop__press_window_control"
TOOL_REPLACE_DESKTOP_TEXT = "mcp__ophira_desktop__replace_window_text"
TOOL_SEND_DESKTOP_KEY = "mcp__ophira_desktop__send_window_key"
TOOL_GENERATE_MEDIA = "mcp__ophira_media__generate_media"
TOOL_PRESENT_EVIDENCE_COMPARISON = (
    "mcp__ophira_presentation__present_evidence_comparison"
)
TOOL_PRESENT_SUPPLIER_COMPARISON = (
    "mcp__ophira_presentation__present_supplier_comparison"
)
TOOL_PRESENT_UNIT_ECONOMICS = (
    "mcp__ophira_presentation__present_unit_economics"
)
TOOL_PRESENT_DECISION_BRIEF = "mcp__ophira_presentation__present_decision_brief"
TOOL_PRESENT_COMMERCIAL_ACTION_DIFF = (
    "mcp__ophira_presentation__present_commercial_action_diff"
)
TOOL_PRESENT_ARTIFACTS = "mcp__ophira_presentation__present_artifacts"
TOOL_READ_COMMERCIAL_TARGET = (
    "mcp__ophira_commercial_actions__read_commercial_target"
)
TOOL_STAGE_COMMERCIAL_ACTION = (
    "mcp__ophira_commercial_actions__stage_commercial_action"
)

LEDGER_TOOL_NAMES = [
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
    TOOL_SEARCH_PROJECT_KNOWLEDGE,
    TOOL_SEARCH_PROJECT_SESSIONS,
    TOOL_READ_SOURCE_RECEIPT,
    TOOL_READ_MARKETPLACE,
    TOOL_READ_GMAIL,
    TOOL_READ_OUTLOOK,
    TOOL_RECORD_HYPOTHESIS,
    TOOL_RECORD_EVIDENCE,
    TOOL_RECORD_EVIDENCE_BATCH,
    TOOL_RECORD_DECISION,
    TOOL_REGISTER_ARTIFACT,
    TOOL_UPDATE_EVIDENCE_STATUS,
]

MEMORY_TOOL_NAMES = [
    TOOL_PROPOSE_MEMORY,
    TOOL_SEARCH_MEMORY,
]

DOMAIN_TOOL_NAMES = [
    TOOL_COMPARE_SUPPLIER_QUOTES,
    TOOL_SEARCH_SUPPLIERS,
    TOOL_DRAFT_OUTBOUND,
    TOOL_RANK_CUSTOMER_CANDIDATES,
    TOOL_RECORD_INTERACTION,
    TOOL_CALCULATE_LANDED_COST,
    TOOL_CALCULATE_UNIT_ECONOMICS,
    TOOL_CASHFLOW_PEAK,
    TOOL_SENSITIVITY_TABLE,
    TOOL_LOOKUP_REQUIREMENTS,
]

BROWSER_TOOL_NAMES = [
    TOOL_SNAPSHOT_PAGE,
    TOOL_ACT_ON_PAGE,
]

DESKTOP_TOOL_NAMES = [
    TOOL_SELECT_DESKTOP_WINDOW,
    TOOL_SNAPSHOT_DESKTOP,
    TOOL_PRESS_DESKTOP,
    TOOL_REPLACE_DESKTOP_TEXT,
    TOOL_SEND_DESKTOP_KEY,
]

MEDIA_TOOL_NAMES = [
    TOOL_GENERATE_MEDIA,
]

PRESENTATION_TOOL_NAMES = [
    TOOL_PRESENT_EVIDENCE_COMPARISON,
    TOOL_PRESENT_SUPPLIER_COMPARISON,
    TOOL_PRESENT_UNIT_ECONOMICS,
    TOOL_PRESENT_DECISION_BRIEF,
    TOOL_PRESENT_COMMERCIAL_ACTION_DIFF,
    TOOL_PRESENT_ARTIFACTS,
]

COMMERCIAL_ACTION_TOOL_NAMES = [
    TOOL_READ_COMMERCIAL_TARGET,
    TOOL_STAGE_COMMERCIAL_ACTION,
]

WEB_TOOL_NAMES = [
    TOOL_SEARCH_PUBLIC_WEB,
    TOOL_FETCH_PAGE_SNAPSHOT,
]

FX_TOOL_NAMES = [TOOL_GET_ECB_FX_REFERENCE]

TOOL_CREATE_SCHEDULE = "mcp__ophira_automation__create_schedule"
TOOL_PAUSE_SCHEDULE = "mcp__ophira_automation__pause_schedule"
TOOL_CREATE_STANDING_INTENT = "mcp__ophira_automation__create_standing_intent"
TOOL_LIST_STANDING_INTENTS = "mcp__ophira_automation__list_standing_intents"
TOOL_TRIGGER_STANDING_INTENT = "mcp__ophira_automation__trigger_standing_intent"
TOOL_SEND_NOTIFICATION = "mcp__ophira_automation__send_notification"
TOOL_RECORD_SCHEDULE_EXPERIENCE = "mcp__ophira_automation__record_schedule_experience"

AUTOMATION_TOOL_NAMES = [
    TOOL_CREATE_SCHEDULE,
    TOOL_PAUSE_SCHEDULE,
    TOOL_CREATE_STANDING_INTENT,
    TOOL_LIST_STANDING_INTENTS,
    TOOL_TRIGGER_STANDING_INTENT,
    TOOL_SEND_NOTIFICATION,
    TOOL_RECORD_SCHEDULE_EXPERIENCE,
]

# §4.10 envelope principle: inbox delivery and budget-throttled intent firing are
# inside an already user-approved run envelope, so unattended scheduled runs can
# deliver results. Cross-envelope commitments (create/pause schedule, create
# intent) still go through the ask flow.
ENVELOPE_APPROVED_MCP_TOOL_NAMES = [
    TOOL_LIST_STANDING_INTENTS,
    TOOL_TRIGGER_STANDING_INTENT,
    TOOL_SEND_NOTIFICATION,
    # Observational data for the current schedule only; never a policy change.
    TOOL_RECORD_SCHEDULE_EXPERIENCE,
]

OPHIRA_TOOL_NAMES = [
    *LEDGER_TOOL_NAMES,
    *MEMORY_TOOL_NAMES,
    *DOMAIN_TOOL_NAMES,
    *WEB_TOOL_NAMES,
    *FX_TOOL_NAMES,
    *AUTOMATION_TOOL_NAMES,
    *BROWSER_TOOL_NAMES,
    *DESKTOP_TOOL_NAMES,
    *MEDIA_TOOL_NAMES,
    *PRESENTATION_TOOL_NAMES,
    *COMMERCIAL_ACTION_TOOL_NAMES,
]

READ_ONLY_MCP_TOOL_NAMES = [
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
    TOOL_SEARCH_PROJECT_KNOWLEDGE,
    TOOL_SEARCH_PROJECT_SESSIONS,
    TOOL_READ_SOURCE_RECEIPT,
    TOOL_READ_MARKETPLACE,
    TOOL_READ_GMAIL,
    TOOL_READ_OUTLOOK,
    TOOL_SEARCH_MEMORY,
    TOOL_COMPARE_SUPPLIER_QUOTES,
    TOOL_SEARCH_SUPPLIERS,
    TOOL_RANK_CUSTOMER_CANDIDATES,
    TOOL_CALCULATE_LANDED_COST,
    TOOL_CALCULATE_UNIT_ECONOMICS,
    TOOL_CASHFLOW_PEAK,
    TOOL_SENSITIVITY_TABLE,
    TOOL_LOOKUP_REQUIREMENTS,
    TOOL_READ_COMMERCIAL_TARGET,
    TOOL_SNAPSHOT_PAGE,
    *PRESENTATION_TOOL_NAMES,
    # FR-23 fallback retrieval: public read-only, same trust level as native Web tools.
    *WEB_TOOL_NAMES,
    *FX_TOOL_NAMES,
]

READ_ONLY_BUILTIN_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
NATIVE_TASK_TRACKING_TOOLS = ["TaskCreate", "TaskGet", "TaskUpdate", "TaskList"]
RESEARCH_READ_TOOL_NAMES = [
    *READ_ONLY_BUILTIN_TOOLS, TOOL_GET_PROJECT_CONTEXT, TOOL_SEARCH_EVIDENCE,
]
# 结构化追问（CLI 内置工具，经 can_use_tool 到达；答案经 updated_input 回传）。
CLARIFYING_TOOL = "AskUserQuestion"
# Plan runs can inspect project files and public sources and ask questions. No
# MCP tool is included: several nominally read-only tools mint ledger receipts.
PLAN_ALLOWED_TOOLS = frozenset({*READ_ONLY_BUILTIN_TOOLS, CLARIFYING_TOOL, "Skill"})
WRITE_BUILTIN_TOOLS = ["Write", "Edit"]
DELETE_BUILTIN_TOOLS = ["Delete"]
SHELL_BUILTIN_TOOLS = ["Bash"]
DELEGATION_TOOL = "Agent"
USER_SCOPE_ID = "default"
