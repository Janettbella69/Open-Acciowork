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


class ProviderErrorCode(StrEnum):
    AUTH_REVOKED_OR_INVALID = "AUTH_REVOKED_OR_INVALID"
    BASE_URL_UNREACHABLE = "BASE_URL_UNREACHABLE"
    MODEL_NOT_AVAILABLE = "MODEL_NOT_AVAILABLE"
    QUOTA_OR_BILLING = "QUOTA_OR_BILLING"
    ANTHROPIC_PROTOCOL_INCOMPATIBLE = "ANTHROPIC_PROTOCOL_INCOMPATIBLE"
    MCP_INIT_FAILED = "MCP_INIT_FAILED"
    SKILL_DISCOVERY_FAILED = "SKILL_DISCOVERY_FAILED"
    SDK_RUNTIME_FAILED = "SDK_RUNTIME_FAILED"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_STATUS = "run.status"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_CANCELLED = "run.cancelled"

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
    PROVIDER_PREFLIGHT = "provider.preflight"

    SCHEDULE_FIRED = "schedule.fired"


class PermissionDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_RUN = "allow_run"
    DENY = "deny"


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


LEDGER_MCP_SERVER_NAME = "ophira_ledger"
MARKET_MCP_SERVER_NAME = "ophira_market"
SUPPLIER_MCP_SERVER_NAME = "ophira_supplier"
CUSTOMER_MCP_SERVER_NAME = "ophira_customer"
COMMERCE_MATH_MCP_SERVER_NAME = "ophira_commerce_math"
WEB_MCP_SERVER_NAME = "ophira_web"
AUTOMATION_MCP_SERVER_NAME = "ophira_automation"

TOOL_GET_PROJECT_CONTEXT = "mcp__ophira_ledger__read_project_context"
TOOL_SEARCH_EVIDENCE = "mcp__ophira_ledger__search_evidence"
TOOL_RECORD_HYPOTHESIS = "mcp__ophira_ledger__record_hypothesis"
TOOL_RECORD_EVIDENCE = "mcp__ophira_ledger__record_evidence"
TOOL_RECORD_EVIDENCE_BATCH = "mcp__ophira_ledger__record_evidence_batch"
TOOL_RECORD_DECISION = "mcp__ophira_ledger__record_decision"
TOOL_UPDATE_EVIDENCE_STATUS = "mcp__ophira_ledger__update_evidence_status"
TOOL_UPDATE_ASSESSMENT = "mcp__ophira_ledger__update_assessment"
TOOL_RECORD_STAGE_DECISION = "mcp__ophira_ledger__record_stage_decision"
TOOL_PROPOSE_MEMORY = "mcp__ophira_ledger__propose_memory"
TOOL_SEARCH_MEMORY = "mcp__ophira_ledger__search_memory"

TOOL_SEARCH_PUBLIC_WEB = "mcp__ophira_web__search_public_web"
TOOL_FETCH_PAGE_SNAPSHOT = "mcp__ophira_web__fetch_page_snapshot"

TOOL_RECORD_MARKET_SIGNAL = "mcp__ophira_market__record_market_signal"
TOOL_SEARCH_MARKET_SIGNALS = "mcp__ophira_market__search_market_signals"
TOOL_COMPARE_SUPPLIER_QUOTES = "mcp__ophira_supplier__compare_supplier_quotes"
TOOL_RANK_CUSTOMER_CANDIDATES = "mcp__ophira_customer__rank_customer_candidates"
TOOL_CALCULATE_LANDED_COST = "mcp__ophira_commerce_math__calculate_landed_cost"
TOOL_CALCULATE_UNIT_ECONOMICS = "mcp__ophira_commerce_math__calculate_unit_economics"

LEDGER_TOOL_NAMES = [
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
    TOOL_RECORD_HYPOTHESIS,
    TOOL_RECORD_EVIDENCE,
    TOOL_RECORD_EVIDENCE_BATCH,
    TOOL_RECORD_DECISION,
    TOOL_UPDATE_EVIDENCE_STATUS,
    TOOL_UPDATE_ASSESSMENT,
    TOOL_RECORD_STAGE_DECISION,
]

MEMORY_TOOL_NAMES = [
    TOOL_PROPOSE_MEMORY,
    TOOL_SEARCH_MEMORY,
]

DOMAIN_TOOL_NAMES = [
    TOOL_RECORD_MARKET_SIGNAL,
    TOOL_SEARCH_MARKET_SIGNALS,
    TOOL_COMPARE_SUPPLIER_QUOTES,
    TOOL_RANK_CUSTOMER_CANDIDATES,
    TOOL_CALCULATE_LANDED_COST,
    TOOL_CALCULATE_UNIT_ECONOMICS,
]

WEB_TOOL_NAMES = [
    TOOL_SEARCH_PUBLIC_WEB,
    TOOL_FETCH_PAGE_SNAPSHOT,
]

TOOL_CREATE_SCHEDULE = "mcp__ophira_automation__create_schedule"
TOOL_PAUSE_SCHEDULE = "mcp__ophira_automation__pause_schedule"
TOOL_CREATE_STANDING_INTENT = "mcp__ophira_automation__create_standing_intent"
TOOL_LIST_STANDING_INTENTS = "mcp__ophira_automation__list_standing_intents"
TOOL_TRIGGER_STANDING_INTENT = "mcp__ophira_automation__trigger_standing_intent"
TOOL_SEND_NOTIFICATION = "mcp__ophira_automation__send_notification"

AUTOMATION_TOOL_NAMES = [
    TOOL_CREATE_SCHEDULE,
    TOOL_PAUSE_SCHEDULE,
    TOOL_CREATE_STANDING_INTENT,
    TOOL_LIST_STANDING_INTENTS,
    TOOL_TRIGGER_STANDING_INTENT,
    TOOL_SEND_NOTIFICATION,
]

# §4.10 envelope principle: inbox delivery and budget-throttled intent firing are
# inside an already user-approved run envelope, so unattended scheduled runs can
# deliver results. Cross-envelope commitments (create/pause schedule, create
# intent) still go through the ask flow.
ENVELOPE_APPROVED_MCP_TOOL_NAMES = [
    TOOL_LIST_STANDING_INTENTS,
    TOOL_TRIGGER_STANDING_INTENT,
    TOOL_SEND_NOTIFICATION,
]

OPHIRA_TOOL_NAMES = [
    *LEDGER_TOOL_NAMES,
    *MEMORY_TOOL_NAMES,
    *DOMAIN_TOOL_NAMES,
    *WEB_TOOL_NAMES,
    *AUTOMATION_TOOL_NAMES,
]

READ_ONLY_MCP_TOOL_NAMES = [
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
    TOOL_SEARCH_MARKET_SIGNALS,
    TOOL_SEARCH_MEMORY,
    TOOL_COMPARE_SUPPLIER_QUOTES,
    TOOL_RANK_CUSTOMER_CANDIDATES,
    TOOL_CALCULATE_LANDED_COST,
    TOOL_CALCULATE_UNIT_ECONOMICS,
    # FR-23 fallback retrieval: public read-only, same trust level as native Web tools.
    *WEB_TOOL_NAMES,
]

READ_ONLY_BUILTIN_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
WRITE_BUILTIN_TOOLS = ["Write", "Edit"]
SHELL_BUILTIN_TOOLS = ["Bash"]
DELEGATION_TOOL = "Agent"

USER_SCOPE_ID = "default"
