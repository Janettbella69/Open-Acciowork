"""Bounded live resources from native SDK observations and frozen Run limits.

Task counters are snapshots, never additive deltas or allocations of Run spend.
No transcript reads, inferred reasoning, provider lookup or new runtime loop.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import Field

from .constants import RESEARCH_UNITS, TERMINAL_RUN_STATUSES, EventType, RunMode, RunStatus
from .events import redact
from .models import APIModel, EventEnvelope, RunRecord, utc_now
from .storage import EventReplayLimitError, FileLedger
from .telemetry import CostTelemetry, TokenTelemetry, build_run_telemetry

MAX_RESOURCE_EVENT_BYTES = 32 * 1024 * 1024
MAX_RESOURCE_EVENTS = 50_000
MAX_RESOURCE_TASKS = 1_000
_MAX_COUNTER = 10**15
_TASK_EVENTS = {EventType.TASK_STARTED, EventType.TASK_PROGRESS, EventType.TASK_COMPLETED}
_AGENT_EVENTS = {EventType.SUBAGENT_STARTED, EventType.SUBAGENT_STOPPED}


class RunResourceLimits(APIModel):
    frozen: bool
    sdk_budget_usd: float | None
    turn_limit: int | None
    wall_clock_deadline: datetime | None


class TaskResourceUsage(APIModel):
    total_tokens: int | None = None
    tool_uses: int | None = None
    duration_ms: int | None = None
    source_sequence: int | None = None
    availability: Literal["reported", "unavailable", "inconsistent"] = "unavailable"


class TaskResourceObservation(APIModel):
    task_id: str
    task_type: str | None = None
    tool_use_id: str | None = None
    requested_agent_type: str | None = None
    description: str | None = None
    status: Literal["running", "completed", "failed", "stopped", "unknown"] = "unknown"
    last_tool_name: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_observed_at: datetime
    usage: TaskResourceUsage = Field(default_factory=TaskResourceUsage)
    warnings: list[str] = Field(default_factory=list)


class AgentResourceObservation(APIModel):
    agent_id: str
    agent_type: str | None = None
    state: Literal["started", "stopped", "unknown"] = "unknown"
    last_observed_at: datetime


class ResearchUnit(APIModel):
    id: str
    description: str


class RunResources(APIModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    project_id: str
    trace_id: str
    mode: RunMode
    status: RunStatus
    observed_at: datetime
    computed_through_sequence: int
    observations_available: bool
    unavailable_reason: str | None = None
    limits: RunResourceLimits
    elapsed_seconds: float | None
    remaining_seconds: float | None
    remaining_budget_usd: float | None
    cost: CostTelemetry
    tokens: TokenTelemetry
    research_units: list[ResearchUnit]
    agents: list[AgentResourceObservation]
    tasks: list[TaskResourceObservation]


def _text(value, maximum=300) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        return None
    return str(redact(value))


def _counter(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNTER


def _task_usage(task: TaskResourceObservation, event: EventEnvelope) -> None:
    raw = event.data.get("usage")
    if raw is None:
        return  # Keep a labeled prior observation, never fill a missing final with zero.
    keys = ("total_tokens", "tool_uses", "duration_ms")
    valid = isinstance(raw, dict) and len(raw) <= 32 and all(
        key not in raw or _counter(raw[key]) for key in keys
    ) and any(key in raw for key in keys)
    if not valid:
        task.warnings.append("invalid_usage")
        task.usage = TaskResourceUsage(
            availability="inconsistent", source_sequence=event.sequence,
        )
        return
    if task.usage.availability == "inconsistent" or any(
        getattr(task.usage, key) is not None and key in raw and raw[key] < getattr(task.usage, key)
        for key in keys
    ):
        task.warnings.append("usage_regressed_or_inconsistent")
        task.usage = TaskResourceUsage(
            availability="inconsistent", source_sequence=event.sequence,
        )
        return
    task.usage = TaskResourceUsage(
        **{key: raw.get(key) for key in keys},
        source_sequence=event.sequence, availability="reported",
    )


def _observations(events: list[EventEnvelope]):
    tasks: dict[str, TaskResourceObservation] = {}
    agents: dict[str, AgentResourceObservation] = {}
    requested: dict[str, set[str]] = {}
    for event in events:
        if event.type in {EventType.TOOL_STARTED, EventType.TOOL_PLANNED}:
            data = event.data
            if data.get("tool_name") in {"Agent", "Task"} and event.tool_use_id:
                invocation = data.get("tool_input")
                unit = (_text(invocation.get("subagent_type"), 100)
                        if isinstance(invocation, dict) else None)
                if unit:
                    requested.setdefault(event.tool_use_id, set()).add(unit)
        if event.type in _AGENT_EVENTS:
            identifier = _text(event.data.get("agent_id"), 160)
            if identifier:
                agents[identifier] = AgentResourceObservation(
                    agent_id=identifier, agent_type=_text(event.data.get("agent_type"), 100),
                    state="started" if event.type == EventType.SUBAGENT_STARTED else "stopped",
                    last_observed_at=event.timestamp,
                )
                if len(agents) > MAX_RESOURCE_TASKS:
                    raise ValueError("native_task_count_limit")
        if event.type not in _TASK_EVENTS:
            continue
        identifier = _text(event.data.get("task_id"), 160)
        if not identifier:
            raise ValueError("invalid_native_task_identity")
        task = tasks.setdefault(identifier, TaskResourceObservation(
            task_id=identifier, last_observed_at=event.timestamp,
        ))
        if event.tool_use_id:
            if task.tool_use_id and task.tool_use_id != event.tool_use_id:
                raise ValueError("conflicting_native_task_identity")
            task.tool_use_id = event.tool_use_id
        task.last_observed_at = event.timestamp
        if event.type == EventType.TASK_STARTED:
            task.task_type = _text(event.data.get("task_type"), 100)
            task.description = _text(event.data.get("description"))
            if task.finished_at is not None:
                task.status = "unknown"
                task.warnings.append("start_after_terminal")
            else:
                task.status = "running"
                task.started_at = task.started_at or event.timestamp
        elif event.type == EventType.TASK_PROGRESS:
            task.description = _text(event.data.get("description")) or task.description
            task.last_tool_name = _text(event.data.get("last_tool_name"), 100)
            if task.finished_at is None:
                task.status = "running"
            else:
                task.warnings.append("progress_after_terminal")
                task.status = "unknown"
            _task_usage(task, event)
        else:
            status = event.data.get("status")
            if status not in {"completed", "failed", "stopped"}:
                task.status = "unknown"
                task.warnings.append("missing_terminal_status")
            elif task.finished_at is not None and task.status != status:
                task.status = "unknown"
                task.warnings.append("conflicting_terminal_status")
            else:
                task.status = status
            task.finished_at = task.finished_at or event.timestamp
            _task_usage(task, event)
        if len(tasks) > MAX_RESOURCE_TASKS or len(agents) > MAX_RESOURCE_TASKS:
            raise ValueError("native_task_count_limit")
    for task in tasks.values():
        labels = requested.get(task.tool_use_id or "", set())
        if len(labels) == 1:
            task.requested_agent_type = next(iter(labels))
        elif labels:
            task.warnings.append("ambiguous_agent_type")
        task.warnings = list(dict.fromkeys(task.warnings))
    return list(tasks.values()), list(agents.values())


def build_run_resources(
    run: RunRecord, events: list[EventEnvelope], *, observed_at: datetime,
    unavailable_reason: str | None = None,
) -> RunResources:
    if any(event.run_id != run.id or event.trace_id != run.trace_id
           or event.project_id != run.project_id for event in events):
        raise ValueError("resource_events_outside_run")
    ordered = sorted(events, key=lambda event: (event.sequence, event.id))
    if ([event.sequence for event in ordered] != list(range(1, len(ordered) + 1))
            or len({event.id for event in ordered}) != len(ordered)):
        unavailable_reason = "incomplete_or_ambiguous_event_log"
    tasks, agents = [], []
    if unavailable_reason is None:
        try:
            tasks, agents = _observations(ordered)
        except ValueError as exc:
            unavailable_reason = str(exc)
    telemetry = build_run_telemetry(run, [] if unavailable_reason else ordered)
    cost = telemetry.cost.estimated_cost_usd.value
    budget = run.sdk_budget_usd
    valid_budget = budget is not None and math.isfinite(budget) and budget > 0
    clock_end = run.finished_at if run.status in TERMINAL_RUN_STATUSES else observed_at
    elapsed = max(0.0, (clock_end - run.started_at).total_seconds()) if (
        clock_end is not None and run.started_at is not None
    ) else None
    remaining = max(0.0, (run.sdk_wall_clock_deadline - observed_at).total_seconds()) if (
        run.sdk_wall_clock_deadline is not None and run.status not in TERMINAL_RUN_STATUSES
    ) else None
    return RunResources(
        run_id=run.id, project_id=run.project_id, trace_id=run.trace_id,
        mode=run.mode, status=run.status, observed_at=observed_at,
        computed_through_sequence=ordered[-1].sequence if ordered else 0,
        observations_available=unavailable_reason is None, unavailable_reason=unavailable_reason,
        limits=RunResourceLimits(
            frozen=valid_budget, sdk_budget_usd=budget if valid_budget else None,
            turn_limit=run.sdk_turn_limit, wall_clock_deadline=run.sdk_wall_clock_deadline,
        ), elapsed_seconds=elapsed, remaining_seconds=remaining,
        remaining_budget_usd=max(0.0, budget - cost) if valid_budget and cost is not None else None,
        cost=telemetry.cost, tokens=telemetry.tokens,
        research_units=[ResearchUnit(id=key, description=value)
                        for key, value in RESEARCH_UNITS.items()]
        if run.mode == RunMode.DEEP_RESEARCH else [], agents=agents, tasks=tasks,
    )


def read_run_resources(
    ledger: FileLedger, run_id: str, *, authorize: Callable[[str], None],
) -> RunResources:
    with ledger._lock_for(run_id):
        run = ledger.get_run(run_id)
        authorize(run.project_id)
        reason = None
        try:
            events = ledger.list_events(
                run_id, max_bytes=MAX_RESOURCE_EVENT_BYTES, max_events=MAX_RESOURCE_EVENTS,
            )
        except EventReplayLimitError:
            events, reason = [], "event_replay_limit"
        except ValueError:
            events, reason = [], "invalid_event_log"
        result = build_run_resources(run, events, observed_at=utc_now(), unavailable_reason=reason)
        authorize(run.project_id)
        return result
