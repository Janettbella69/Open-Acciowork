"""Bounded project metrics from existing Run records and observable SDK events.

No provider lookup, model call, business-content export, or new logging pipeline.
Run costs belong to logical Runs; model/Skill/tool participation is not cost attribution.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from .constants import OPHIRA_TOOL_NAMES, EventType, ProviderErrorCode, RunStatus
from .events import redact
from .models import APIModel, EventEnvelope, RunRecord
from .storage import EventReplayBudget, EventReplayLimitError, FileLedger
from .telemetry import RunTelemetry, build_run_telemetry

MAX_METRICS_DAYS = 31
MAX_METRICS_RUNS = 200
MAX_RUN_EVENT_BYTES = 4 * 1024 * 1024
MAX_RUN_EVENTS = 10_000
MAX_REQUEST_EVENT_BYTES = 32 * 1024 * 1024
MAX_REQUEST_EVENTS = 50_000
MAX_DIMENSION_GROUPS = 100
_MCP_NAMES = frozenset(name for name in OPHIRA_TOOL_NAMES if name.startswith("mcp__"))
_ERROR_CODES = frozenset(ProviderErrorCode)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}")


class Distribution(APIModel):
    observed_count: int
    unavailable_count: int
    observed_total: float | None
    p50: float | None
    p95: float | None


class FailureRate(APIModel):
    numerator: int
    denominator: int
    value: float | None


class RunMetrics(APIModel):
    run_count: int
    status_counts: dict[str, int]
    execution_failure_rate: FailureRate
    duration_ms: Distribution
    queue_duration_ms: Distribution
    ttft_ms: Distribution
    cost_estimate_usd: Distribution | None
    cost_basis_counts: dict[str, int]


class RunMetricGroup(APIModel):
    dimensions: dict[str, str | None]
    runs: RunMetrics


class InvocationMetrics(APIModel):
    invocation_count: int
    completed_count: int
    failed_count: int
    unknown_count: int
    pending_count: int
    failure_rate: FailureRate
    duration_ms: Distribution
    queue_duration_ms: Distribution


class InvocationMetricGroup(APIModel):
    identifier: str | None
    run_count: int
    invocations: InvocationMetrics


class ErrorMetricGroup(APIModel):
    code: ProviderErrorCode | None
    failed_run_count: int


class MetricsWindow(APIModel):
    created_since: datetime
    created_until: datetime
    observed_at: datetime
    selection_basis: Literal["run.created_at"] = "run.created_at"


class MetricsCoverage(APIModel):
    matching_runs: int = 0
    selected_runs: int = 0
    omitted_by_run_limit: int = 0
    invalid_created_at_runs: int = 0
    unavailable_event_replays: int = 0
    unavailable_reasons: dict[str, int] = Field(default_factory=dict)
    event_bytes_read: int = 0
    event_count_read: int = 0
    unattributed_tool_events: int = 0
    unclassified_tool_invocations: int = 0
    group_overflow_associations: int = 0
    run_limit: int = MAX_METRICS_RUNS
    per_run_event_byte_limit: int = MAX_RUN_EVENT_BYTES
    per_run_event_limit: int = MAX_RUN_EVENTS
    request_event_byte_limit: int = MAX_REQUEST_EVENT_BYTES
    request_event_limit: int = MAX_REQUEST_EVENTS


class ProjectMetrics(APIModel):
    schema_version: Literal["1"] = "1"
    project_id: str
    window: MetricsWindow
    coverage: MetricsCoverage
    summary: RunMetrics
    providers: list[RunMetricGroup]
    models: list[RunMetricGroup]
    skills: list[InvocationMetricGroup]
    mcp_tools: list[InvocationMetricGroup]
    errors: list[ErrorMetricGroup]
    business_outcome_available: Literal[False] = False


@dataclass(frozen=True)
class MetricsSource:
    run: RunRecord
    events: list[EventEnvelope] | None
    unavailable_reason: str = "event_replay_unavailable"


def _identifier(value: object) -> str | None:
    # Return identifiers only, never display names, URLs, raw errors or redacted
    # replacements. Unrecognized strings share an explicit unknown bucket.
    if (not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
            or "://" in value or redact(value) != value
            or value.startswith(("sk-", "ghp_", "github_pat_"))):
        return None
    return value


def _distribution(values: list[int | float | None]) -> Distribution:
    known = sorted(float(value) for value in values if isinstance(value, int | float)
                   and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
    return Distribution(
        observed_count=len(known), unavailable_count=len(values) - len(known),
        observed_total=math.fsum(known) if known else None,
        p50=known[math.ceil(len(known) * 0.5) - 1] if known else None,
        p95=known[math.ceil(len(known) * 0.95) - 1] if known else None,
    )


def _failure_rate(failed: int, completed: int) -> FailureRate:
    denominator = failed + completed
    return FailureRate(numerator=failed, denominator=denominator,
                       value=failed / denominator if denominator else None)


def _run_metrics(rows: list[tuple[RunRecord, RunTelemetry]], *, costs: bool) -> RunMetrics:
    statuses = Counter(run.status.value for run, _ in rows)
    return RunMetrics(
        run_count=len(rows), status_counts=dict(sorted(statuses.items())),
        execution_failure_rate=_failure_rate(statuses[RunStatus.FAILED],
                                             statuses[RunStatus.COMPLETED]),
        duration_ms=_distribution([item.latency.total_task_duration_ms.value for _, item in rows]),
        queue_duration_ms=_distribution([
            item.latency.run_queue_duration_ms.value for _, item in rows
        ]),
        ttft_ms=_distribution([item.latency.ttft_ms.value for _, item in rows]),
        cost_estimate_usd=(_distribution([
            item.cost.estimated_cost_usd.value for _, item in rows
        ]) if costs else None),
        cost_basis_counts=(dict(Counter(
            "sdk_anthropic_list_price" if item.cost.cost_basis.value == "sdk_anthropic_list_price"
            else "unknown" for _, item in rows
        )) if costs else {}),
    )


def _trusted_replay(source: MetricsSource) -> tuple[RunTelemetry, str | None]:
    run, events = source.run, source.events
    if events is None:
        return build_run_telemetry(run, []), source.unavailable_reason
    if (len(events) > MAX_RUN_EVENTS or any(
        event.run_id != run.id or event.project_id != run.project_id
        or event.trace_id != run.trace_id or not 1 <= event.sequence <= MAX_RUN_EVENTS
        for event in events
    )):
        return build_run_telemetry(run, []), "inconsistent_event_replay"
    result = build_run_telemetry(run, events)
    if result.event_log_complete_and_unambiguous.value is not True:
        return build_run_telemetry(run, []), "inconsistent_event_replay"
    return result, None


def _skill_identifier(run: RunRecord, events: list[EventEnvelope]) -> str | None:
    names = set()
    for event in events:
        if event.type != EventType.TOOL_STARTED:
            continue
        payload = event.data.get("tool_input")
        name = payload.get("skill") if isinstance(payload, dict) else None
        if (name := _identifier(name)) is None:
            return None
        names.add(name)
    if len(names) != 1:
        return None
    requested = names.pop()
    pinned = {_identifier(item.get("id")) for item in run.skill_snapshot}
    if requested in pinned:
        return requested
    matches = [item for item in pinned if item and item.rsplit(":", 1)[-1] == requested]
    return matches[0] if len(matches) == 1 else None


def _invocation_groups(groups: dict) -> list[InvocationMetricGroup]:
    result = []
    for identifier, entries in sorted(groups.items(), key=lambda item: item[0] or ""):
        states = Counter(entry[1] for entry in entries)
        result.append(InvocationMetricGroup(
            identifier=identifier, run_count=len({entry[0] for entry in entries}),
            invocations=InvocationMetrics(
                invocation_count=len(entries), completed_count=states["completed"],
                failed_count=states["failed"], unknown_count=states["unknown"],
                pending_count=states["pending"],
                failure_rate=_failure_rate(states["failed"], states["completed"]),
                duration_ms=_distribution([entry[2] for entry in entries]),
                queue_duration_ms=_distribution([entry[3] for entry in entries]),
            ),
        ))
    return result


def _bucket(groups: dict, key, unknown, coverage: MetricsCoverage):
    known_count = len(groups) - int(unknown in groups)
    if key not in groups and key != unknown and known_count >= MAX_DIMENSION_GROUPS:
        coverage.group_overflow_associations += 1
        return unknown
    return key


def build_project_metrics(
    project_id: str, sources: list[MetricsSource], *, created_since: datetime,
    created_until: datetime, observed_at: datetime, coverage: MetricsCoverage | None = None,
) -> ProjectMetrics:
    """Pure aggregation of already selected sources; never return event payloads."""
    if len(sources) > MAX_METRICS_RUNS:
        raise ValueError("project metrics exceed the Run bound")
    if len({source.run.id for source in sources}) != len(sources):
        raise ValueError("project metrics contain duplicate Run records")
    if any(source.run.project_id != project_id for source in sources):
        raise ValueError("project metrics source belongs to another project")
    coverage = coverage.model_copy(deep=True) if coverage else MetricsCoverage(
        matching_runs=len(sources), selected_runs=len(sources),
    )
    rows = []
    providers, models, skills, mcp_tools = (defaultdict(list) for _ in range(4))
    errors: Counter = Counter()
    unavailable = Counter(coverage.unavailable_reasons)
    for source in sorted(sources, key=lambda item: item.run.id):
        run = source.run
        telemetry, reason = _trusted_replay(source)
        rows.append((run, telemetry))
        if reason:
            coverage.unavailable_event_replays += 1
            unavailable[reason] += 1
        snapshot = run.provider_snapshot or {}
        provider_id = _identifier(snapshot.get("id"))
        if provider_id != _identifier(run.provider_profile_id):
            provider_id = None
        provider_key = (provider_id, _identifier(snapshot.get("model")))
        provider_key = _bucket(providers, provider_key, (None, None), coverage)
        providers[provider_key].append((run, telemetry))
        observed_models = {
            _identifier(name) for name in telemetry.provider.observed_models.value or []
        }
        model_keys = set()
        for name in sorted(observed_models or {None}, key=lambda value: value or ""):
            name = _bucket(models, name, None, coverage)
            if name not in model_keys:
                models[name].append((run, telemetry))
                model_keys.add(name)
        if run.status == RunStatus.FAILED:
            code = run.error_code if run.error_code in _ERROR_CODES else None
            if code is None and reason is None:
                observed_codes = {event.data.get("error_code") for event in source.events or []
                                  if event.type == EventType.RUN_FAILED
                                  and isinstance(event.data.get("error_code"), str)
                                  and event.data["error_code"] in _ERROR_CODES}
                code = next(iter(observed_codes)) if len(observed_codes) == 1 else None
            errors[code] += 1
        if reason:
            continue
        events_by_tool: dict[str, list[EventEnvelope]] = defaultdict(list)
        for event in source.events or []:
            if event.tool_use_id:
                events_by_tool[event.tool_use_id].append(event)
        coverage.unattributed_tool_events += len(telemetry.tools.unattributed_event_sequences)
        for timing in telemetry.tools.timings:
            # A planned call is not execution. Multiple starts remain one
            # invocation with an ambiguous outcome, rather than several successes.
            if not timing.started_event_sequences:
                continue
            lifecycle = events_by_tool[timing.tool_use_id]
            names = {event.data.get("tool_name") for event in lifecycle
                     if isinstance(event.data.get("tool_name"), str)}
            name = timing.tool_name.value
            if "Skill" in names:
                groups = skills
                identifier = _skill_identifier(run, lifecycle) if name == "Skill" else None
            elif any(item.startswith("mcp__") for item in names):
                groups = mcp_tools
                identifier = name if name in _MCP_NAMES else None
            else:
                if name is None:
                    coverage.unclassified_tool_invocations += 1
                continue
            identifier = _bucket(groups, identifier, None, coverage)
            state = timing.lifecycle_status
            if identifier is None or state == "ambiguous":
                state = "unknown"
            elif state not in {"completed", "failed"}:
                state = "pending"
            elif state == "completed":
                terminal = next(event for event in lifecycle
                                if event.sequence in timing.terminal_event_sequences)
                response = terminal.data.get("tool_response")
                if isinstance(response, dict):
                    flags = [response[key] for key in ("isError", "is_error") if key in response]
                    if any(not isinstance(value, bool) for value in flags):
                        state = "unknown"
                    elif flags and any(value != flags[0] for value in flags):
                        state = "unknown"
                    elif flags and flags[0]:
                        state = "failed"
            groups[identifier].append((
                run.id, state,
                timing.run_duration_ms.value if state != "unknown" else None,
                timing.queue_duration_ms.value if state != "unknown" else None,
            ))
    coverage.unavailable_reasons = dict(sorted(unavailable.items()))
    return ProjectMetrics(
        project_id=project_id,
        window=MetricsWindow(created_since=created_since, created_until=created_until,
                             observed_at=observed_at),
        coverage=coverage, summary=_run_metrics(rows, costs=True),
        providers=[RunMetricGroup(dimensions={"provider_profile_id": key[0],
                                              "configured_model": key[1]},
                                  runs=_run_metrics(items, costs=True))
                   for key, items in sorted(providers.items(), key=lambda item: str(item[0]))],
        models=[RunMetricGroup(dimensions={"observed_model": name},
                               runs=_run_metrics(items, costs=False))
                for name, items in sorted(models.items(), key=lambda item: item[0] or "")],
        skills=_invocation_groups(skills), mcp_tools=_invocation_groups(mcp_tools),
        errors=[ErrorMetricGroup(code=code, failed_run_count=count)
                for code, count in sorted(errors.items(), key=lambda item: item[0] or "")],
    )


def read_project_metrics(
    ledger: FileLedger, project_id: str, *, created_since: datetime,
    created_until: datetime, observed_at: datetime, max_runs: int = MAX_METRICS_RUNS,
) -> ProjectMetrics:
    """Read only the authorized project, with per-Run and whole-request replay limits."""
    if any(value.tzinfo is None or value.utcoffset() is None
           for value in (created_since, created_until, observed_at)):
        raise ValueError("metrics window boundaries require a timezone")
    created_since, created_until, observed_at = (
        value.astimezone(UTC) for value in (created_since, created_until, observed_at)
    )
    horizon = (created_until - created_since).total_seconds()
    if not 1 <= max_runs <= MAX_METRICS_RUNS or not 0 < horizon <= MAX_METRICS_DAYS * 86_400:
        raise ValueError("metrics window or Run limit is invalid")
    project = ledger.get_project(project_id)
    candidates = []
    coverage = MetricsCoverage(run_limit=max_runs)
    for run in ledger.list_runs(project_id, sort_by_created_at=False):
        if (run.project_id != project.id or run.tenant_id != project.tenant_id
                or run.identity_binding != project.identity_binding):
            raise ValueError("project metrics Run identity is inconsistent")
        if run.created_at.tzinfo is None or run.created_at.utcoffset() is None:
            coverage.invalid_created_at_runs += 1
        elif created_since <= run.created_at < created_until:
            candidates.append(run)
    candidates.sort(key=lambda run: (run.created_at, run.id), reverse=True)
    coverage.matching_runs = len(candidates)
    selected = candidates[:max_runs]
    coverage.selected_runs = len(selected)
    coverage.omitted_by_run_limit = len(candidates) - len(selected)
    budget = EventReplayBudget(max_bytes=MAX_REQUEST_EVENT_BYTES, max_events=MAX_REQUEST_EVENTS)
    sources = []
    for run in selected:
        try:
            events = ledger.list_events(run.id, max_bytes=MAX_RUN_EVENT_BYTES,
                                        max_events=MAX_RUN_EVENTS, budget=budget)
            sources.append(MetricsSource(run, events))
        except EventReplayLimitError as exc:
            sources.append(MetricsSource(run, None, exc.reason))
        except (OSError, ValueError):
            sources.append(MetricsSource(run, None, "event_replay_unavailable"))
    coverage.event_bytes_read, coverage.event_count_read = budget.bytes_read, budget.events_read
    return build_project_metrics(project_id, sources, created_since=created_since,
                                 created_until=created_until, observed_at=observed_at,
                                 coverage=coverage)
