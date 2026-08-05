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
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


LEAD_AGENT = "opportunity-lead"
AGENT_NAMES = {
    Dimension.DEMAND: "demand-agent",
    Dimension.SUPPLY: "supply-agent",
    Dimension.CAPITAL: "capital-agent",
    Dimension.SALES: "sales-agent",
}

MCP_SERVER_NAME = "ophira"
TOOL_GET_PROJECT_CONTEXT = "mcp__ophira__get_project_context"
TOOL_RECORD_HYPOTHESIS = "mcp__ophira__record_hypothesis"
TOOL_RECORD_EVIDENCE = "mcp__ophira__record_evidence"
TOOL_UPDATE_ASSESSMENT = "mcp__ophira__update_assessment"
TOOL_RECORD_STAGE_DECISION = "mcp__ophira__record_stage_decision"
TOOL_PROPOSE_MEMORY = "mcp__ophira__propose_memory"
TOOL_SEARCH_MEMORY = "mcp__ophira__search_memory"

LEDGER_TOOL_NAMES = [
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_RECORD_HYPOTHESIS,
    TOOL_RECORD_EVIDENCE,
    TOOL_UPDATE_ASSESSMENT,
    TOOL_RECORD_STAGE_DECISION,
]

MEMORY_TOOL_NAMES = [
    TOOL_PROPOSE_MEMORY,
    TOOL_SEARCH_MEMORY,
]

OPHIRA_TOOL_NAMES = [*LEDGER_TOOL_NAMES, *MEMORY_TOOL_NAMES]

READ_ONLY_BUILTIN_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
WRITE_BUILTIN_TOOLS = ["Write", "Edit"]
SHELL_BUILTIN_TOOLS = ["Bash"]
DELEGATION_TOOL = "Agent"

USER_SCOPE_ID = "default"
