"""Deterministic run telemetry derived from the durable run/event ledger.

The aggregator is deliberately pure: it does not read a provider, call the SDK, or
consult mutable project state.  Replaying the same ``RunRecord`` and event envelopes
therefore produces byte-for-byte equivalent JSON.  Missing or ambiguous observations
stay explicitly unavailable instead of being replaced with zeroes or guessed pairings.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import Field, model_validator

from .constants import TERMINAL_RUN_STATUSES, EventType, RunStatus
from .models import APIModel, EventEnvelope, RunRecord
from .presentation_tools import presentation_event_response

TELEMETRY_SCHEMA_VERSION = "1"
MAX_MODEL_USAGE_MODELS = 64
MAX_USAGE_FIELDS = 32
MAX_MODEL_NAME_LENGTH = 300
MAX_USAGE_KEY_LENGTH = 100
MAX_USAGE_STRING_LENGTH = 1_000
MAX_TOKEN_COUNT = 10**15
MAX_COST_USD = 10**12

_TOOL_LIFECYCLE_TYPES = frozenset(
    {
        EventType.TOOL_PLANNED.value,
        EventType.TOOL_STARTED.value,
        EventType.TOOL_COMPLETED.value,
        EventType.TOOL_FAILED.value,
    }
)
_TOOL_TERMINAL_TYPES = frozenset(
    {EventType.TOOL_COMPLETED.value, EventType.TOOL_FAILED.value}
)
_PROGRESS_TYPES = frozenset(
    {
        *_TOOL_LIFECYCLE_TYPES,
        EventType.SUBAGENT_STARTED.value,
        EventType.SUBAGENT_STOPPED.value,
        EventType.TASK_STARTED.value,
        EventType.TASK_PROGRESS.value,
        EventType.TASK_COMPLETED.value,
    }
)

MetricValue = TypeVar("MetricValue")


class TelemetryMetric(APIModel, Generic[MetricValue]):
    """One observed measurement or an explicit statement that it is unavailable."""

    value: MetricValue | None = None
    availability: Literal["observed", "unavailable"]
    unit: str
    source: str
    source_event_sequences: list[int] = Field(default_factory=list)
    scope: str | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def coherent_availability(self) -> TelemetryMetric[MetricValue]:
        if self.availability == "observed":
            if self.value is None:
                raise ValueError("an observed telemetry metric requires a value")
            if self.unavailable_reason is not None:
                raise ValueError("an observed telemetry metric cannot have a missing reason")
        else:
            if self.value is not None:
                raise ValueError("an unavailable telemetry metric cannot have a value")
            if not self.unavailable_reason:
                raise ValueError("an unavailable telemetry metric requires a reason")
        return self


class TelemetryWarning(APIModel):
    code: str
    detail: str
    event_sequences: list[int] = Field(default_factory=list)


class ToolTiming(APIModel):
    tool_use_id: str
    tool_name: TelemetryMetric[str]
    lifecycle_status: Literal["planned", "running", "completed", "failed", "ambiguous"]
    planned_event_sequences: list[int] = Field(default_factory=list)
    started_event_sequences: list[int] = Field(default_factory=list)
    terminal_event_sequences: list[int] = Field(default_factory=list)
    queue_duration_ms: TelemetryMetric[int]
    run_duration_ms: TelemetryMetric[int]
    quality_warnings: list[str] = Field(default_factory=list)


class DurationAggregate(APIModel):
    observed_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    observed_total_ms: TelemetryMetric[int]
    observed_max_ms: TelemetryMetric[int]


class ToolTelemetry(APIModel):
    timings: list[ToolTiming] = Field(default_factory=list)
    unattributed_event_sequences: list[int] = Field(default_factory=list)
    queue_durations: DurationAggregate
    run_durations: DurationAggregate


class RunLatencyTelemetry(APIModel):
    run_queue_duration_ms: TelemetryMetric[int]
    total_task_duration_ms: TelemetryMetric[int]
    sdk_duration_ms: TelemetryMetric[int]
    sdk_api_duration_ms: TelemetryMetric[int]
    ttft_ms: TelemetryMetric[int]
    first_visible_text_ms: TelemetryMetric[int]
    first_progress_ms: TelemetryMetric[int]
    first_useful_typed_card_ms: TelemetryMetric[int]
    first_useful_typed_card_component: TelemetryMetric[str]


class TokenTelemetry(APIModel):
    input_tokens: TelemetryMetric[int]
    output_tokens: TelemetryMetric[int]
    cache_read_input_tokens: TelemetryMetric[int]
    cache_creation_input_tokens: TelemetryMetric[int]
    cache_hit: TelemetryMetric[bool]


class CostTelemetry(APIModel):
    estimated_cost_usd: TelemetryMetric[float]
    cost_basis: TelemetryMetric[str]


class ProviderTelemetry(APIModel):
    provider_profile_id: TelemetryMetric[str]
    provider_display_name: TelemetryMetric[str]
    configured_model: TelemetryMetric[str]
    observed_models: TelemetryMetric[list[str]]


class OutcomeTelemetry(APIModel):
    run_status: RunStatus
    completed: TelemetryMetric[bool]
    result_subtype: TelemetryMetric[str]
    result_is_error: TelemetryMetric[bool]
    turn_count: TelemetryMetric[int]


class RunTelemetry(APIModel):
    schema_version: Literal["1"] = TELEMETRY_SCHEMA_VERSION
    run_id: str
    project_id: str
    trace_id: str
    computed_through_sequence: int = Field(ge=0)
    event_log_complete_and_unambiguous: TelemetryMetric[bool]
    prompt_prefix_hash: TelemetryMetric[str]
    latency: RunLatencyTelemetry
    tools: ToolTelemetry
    tokens: TokenTelemetry
    cost: CostTelemetry
    provider: ProviderTelemetry
    outcome: OutcomeTelemetry
    quality_warnings: list[TelemetryWarning] = Field(default_factory=list)


def _observed(
    value: MetricValue,
    *,
    unit: str,
    source: str,
    sequences: list[int] | None = None,
    scope: str | None = None,
) -> TelemetryMetric[MetricValue]:
    return TelemetryMetric[MetricValue](
        value=value,
        availability="observed",
        unit=unit,
        source=source,
        source_event_sequences=sequences or [],
        scope=scope,
    )


def _unavailable(
    *,
    unit: str,
    source: str,
    reason: str,
    sequences: list[int] | None = None,
    scope: str | None = None,
) -> TelemetryMetric:
    return TelemetryMetric(
        value=None,
        availability="unavailable",
        unit=unit,
        source=source,
        source_event_sequences=sequences or [],
        scope=scope,
        unavailable_reason=reason,
    )


def _duration_between(
    start: datetime | None,
    end: datetime | None,
    *,
    source: str,
    sequences: list[int] | None = None,
    missing_reason: str,
) -> TelemetryMetric[int]:
    if start is None or end is None:
        return _unavailable(
            unit="milliseconds",
            source=source,
            sequences=sequences,
            reason=missing_reason,
        )
    start_aware = start.tzinfo is not None and start.utcoffset() is not None
    end_aware = end.tzinfo is not None and end.utcoffset() is not None
    if start_aware != end_aware:
        return _unavailable(
            unit="milliseconds",
            source=source,
            sequences=sequences,
            reason="timestamps use incompatible timezone awareness",
        )
    elapsed_ms = round((end - start).total_seconds() * 1_000)
    if elapsed_ms < 0:
        return _unavailable(
            unit="milliseconds",
            source=source,
            sequences=sequences,
            reason="terminal timestamp predates start timestamp",
        )
    return _observed(
        elapsed_ms,
        unit="milliseconds",
        source=source,
        sequences=sequences,
    )


def _valid_non_negative_int(value: object, *, maximum: int = MAX_TOKEN_COUNT) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= maximum
    )


def _valid_cost(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_COST_USD
    )


def _bounded_usage(value: object) -> dict[str, int | float | str | bool | None] | None:
    """Accept only the SDK's shallow scalar usage object; never recurse unbounded data."""

    if not isinstance(value, dict) or len(value) > MAX_USAGE_FIELDS:
        return None
    bounded: dict[str, int | float | str | bool | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > MAX_USAGE_KEY_LENGTH:
            return None
        if item is not None and not isinstance(item, str | int | float | bool):
            return None
        if isinstance(item, str) and len(item) > MAX_USAGE_STRING_LENGTH:
            return None
        if (
            isinstance(item, int | float)
            and not isinstance(item, bool)
            and (not math.isfinite(item) or abs(item) > MAX_TOKEN_COUNT)
        ):
            return None
        bounded[key] = item
    return bounded


def _bounded_model_usage(
    value: object,
) -> dict[str, dict[str, int | float | str | bool | None]] | None:
    """Validate the documented two-level model-usage shape before aggregation."""

    if not isinstance(value, dict) or not value or len(value) > MAX_MODEL_USAGE_MODELS:
        return None
    bounded: dict[str, dict[str, int | float | str | bool | None]] = {}
    for model, usage in value.items():
        if (
            not isinstance(model, str)
            or not model
            or len(model) > MAX_MODEL_NAME_LENGTH
        ):
            return None
        parsed = _bounded_usage(usage)
        if parsed is None:
            return None
        bounded[model] = parsed
    return bounded


def _event_metric(
    event: EventEnvelope | None,
    started_at: datetime | None,
    *,
    source: str,
    missing_reason: str,
    log_trustworthy: bool,
) -> TelemetryMetric[int]:
    if not log_trustworthy:
        return _unavailable(
            unit="milliseconds",
            source=source,
            reason="event log is incomplete or has duplicate sequence numbers",
        )
    if event is None:
        return _unavailable(
            unit="milliseconds",
            source=source,
            reason=missing_reason,
        )
    return _duration_between(
        started_at,
        event.timestamp,
        source=source,
        sequences=[event.sequence],
        missing_reason="run.started_at is unavailable",
    )


def _first_event(
    events: list[EventEnvelope],
    predicate,
) -> EventEnvelope | None:
    return next((event for event in events if predicate(event)), None)


def _text_field(event: EventEnvelope, field: str) -> str | None:
    value = event.data.get(field)
    return value if isinstance(value, str) else None


def _is_valid_typed_card(event: EventEnvelope, project_id: str) -> bool:
    """Validate only the bounded, direct UI envelope emitted by the runtime."""

    if len(event.data) > 16:
        return False
    presentation = event.data.get("presentation")
    if not isinstance(presentation, dict) or len(presentation) > 16:
        return False
    payload = presentation.get("payload")
    if not isinstance(payload, dict) or len(payload) > 32:
        return False
    return presentation_event_response(
        {"presentation": presentation}, project_id
    ) is not None


def _tool_timing(tool_use_id: str, events: list[EventEnvelope]) -> ToolTiming:
    planned = [event for event in events if event.type == EventType.TOOL_PLANNED.value]
    started = [event for event in events if event.type == EventType.TOOL_STARTED.value]
    terminal = [event for event in events if event.type in _TOOL_TERMINAL_TYPES]
    names = sorted(
        {
            name
            for event in events
            if (name := _text_field(event, "tool_name")) is not None
            and name
            and len(name) <= MAX_MODEL_NAME_LENGTH
        }
    )
    warnings: list[str] = []
    if len(names) == 1:
        tool_name = _observed(
            names[0],
            unit="identifier",
            source="tool lifecycle event data.tool_name",
            sequences=[event.sequence for event in events],
        )
    elif not names:
        tool_name = _unavailable(
            unit="identifier",
            source="tool lifecycle event data.tool_name",
            reason="no non-empty tool name was recorded",
            sequences=[event.sequence for event in events],
        )
        warnings.append("missing_tool_name")
    else:
        tool_name = _unavailable(
            unit="identifier",
            source="tool lifecycle event data.tool_name",
            reason="events sharing the tool_use_id contain conflicting tool names",
            sequences=[event.sequence for event in events],
        )
        warnings.append("conflicting_tool_names")

    if len(planned) != 1:
        queue_duration = _unavailable(
            unit="milliseconds",
            source="tool.planned→tool.started",
            reason=(
                "tool.planned event is unavailable"
                if not planned
                else "multiple tool.planned events make queue timing ambiguous"
            ),
            sequences=[event.sequence for event in [*planned, *started]],
        )
        warnings.append("missing_tool_planned" if not planned else "duplicate_tool_planned")
    elif len(started) != 1:
        queue_duration = _unavailable(
            unit="milliseconds",
            source="tool.planned→tool.started",
            reason=(
                "tool.started event is unavailable"
                if not started
                else "multiple tool.started events make queue timing ambiguous"
            ),
            sequences=[event.sequence for event in [*planned, *started]],
        )
        warnings.append("missing_tool_started" if not started else "duplicate_tool_started")
    elif started[0].sequence <= planned[0].sequence:
        queue_duration = _unavailable(
            unit="milliseconds",
            source="tool.planned→tool.started",
            reason="tool lifecycle sequence is out of order",
            sequences=[planned[0].sequence, started[0].sequence],
        )
        warnings.append("tool_lifecycle_out_of_order")
    else:
        queue_duration = _duration_between(
            planned[0].timestamp,
            started[0].timestamp,
            source="tool.planned→tool.started",
            sequences=[planned[0].sequence, started[0].sequence],
            missing_reason="tool queue timestamps are unavailable",
        )
        if queue_duration.availability == "unavailable":
            warnings.append("invalid_tool_queue_timestamps")

    if len(started) != 1:
        run_duration = _unavailable(
            unit="milliseconds",
            source="tool.started→tool.completed|tool.failed",
            reason=(
                "tool.started event is unavailable"
                if not started
                else "multiple tool.started events make run timing ambiguous"
            ),
            sequences=[event.sequence for event in [*started, *terminal]],
        )
    elif len(terminal) != 1:
        run_duration = _unavailable(
            unit="milliseconds",
            source="tool.started→tool.completed|tool.failed",
            reason=(
                "tool terminal event is unavailable"
                if not terminal
                else "multiple tool terminal events make run timing ambiguous"
            ),
            sequences=[event.sequence for event in [*started, *terminal]],
        )
        warnings.append("missing_tool_terminal" if not terminal else "duplicate_tool_terminal")
    elif terminal[0].sequence <= started[0].sequence:
        run_duration = _unavailable(
            unit="milliseconds",
            source="tool.started→tool.completed|tool.failed",
            reason="tool lifecycle sequence is out of order",
            sequences=[started[0].sequence, terminal[0].sequence],
        )
        if "tool_lifecycle_out_of_order" not in warnings:
            warnings.append("tool_lifecycle_out_of_order")
    else:
        run_duration = _duration_between(
            started[0].timestamp,
            terminal[0].timestamp,
            source="tool.started→tool.completed|tool.failed",
            sequences=[started[0].sequence, terminal[0].sequence],
            missing_reason="tool run timestamps are unavailable",
        )
        if run_duration.availability == "unavailable":
            warnings.append("invalid_tool_run_timestamps")

    duplicate_phase = len(planned) > 1 or len(started) > 1 or len(terminal) > 1
    if duplicate_phase or len(names) > 1:
        status: Literal["planned", "running", "completed", "failed", "ambiguous"] = (
            "ambiguous"
        )
    elif terminal:
        status = "failed" if terminal[0].type == EventType.TOOL_FAILED.value else "completed"
    elif started:
        status = "running"
    else:
        status = "planned"

    return ToolTiming(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        lifecycle_status=status,
        planned_event_sequences=[event.sequence for event in planned],
        started_event_sequences=[event.sequence for event in started],
        terminal_event_sequences=[event.sequence for event in terminal],
        queue_duration_ms=queue_duration,
        run_duration_ms=run_duration,
        quality_warnings=warnings,
    )


def _duration_aggregate(
    timings: list[ToolTiming],
    field: Literal["queue_duration_ms", "run_duration_ms"],
) -> DurationAggregate:
    metrics = [getattr(timing, field) for timing in timings]
    values = [metric.value for metric in metrics if metric.availability == "observed"]
    observed_values = [value for value in values if isinstance(value, int)]
    unavailable_count = len(metrics) - len(observed_values)
    source = f"tools.timings[*].{field}"
    if not observed_values:
        total = _unavailable(
            unit="milliseconds",
            source=source,
            reason="no unambiguous paired tool timings were observed",
        )
        maximum = _unavailable(
            unit="milliseconds",
            source=source,
            reason="no unambiguous paired tool timings were observed",
        )
    else:
        sequences = sorted(
            {
                sequence
                for metric in metrics
                if metric.availability == "observed"
                for sequence in metric.source_event_sequences
            }
        )
        total = _observed(
            sum(observed_values),
            unit="milliseconds",
            source=source,
            sequences=sequences,
            scope="observed_spans_only",
        )
        maximum = _observed(
            max(observed_values),
            unit="milliseconds",
            source=source,
            sequences=sequences,
            scope="observed_spans_only",
        )
    return DurationAggregate(
        observed_count=len(observed_values),
        unavailable_count=unavailable_count,
        observed_total_ms=total,
        observed_max_ms=maximum,
    )


def _usage_metric(
    *,
    model_usage: dict[str, dict[str, int | float | str | bool | None]] | None,
    usage: dict[str, int | float | str | bool | None] | None,
    model_key: str,
    usage_key: str,
    result_sequence: int | None,
    crash_zeroed: bool,
) -> TelemetryMetric[int]:
    sequences = [result_sequence] if result_sequence is not None else []
    if crash_zeroed:
        return _unavailable(
            unit="tokens",
            source="run.result",
            sequences=sequences,
            reason="SDK error_during_execution result may contain zeroed accounting",
        )
    if model_usage is not None:
        values = [item.get(model_key) for item in model_usage.values()]
        if values and all(_valid_non_negative_int(item) for item in values):
            total = sum(int(item) for item in values)
            if total > MAX_TOKEN_COUNT:
                return _unavailable(
                    unit="tokens",
                    source=f"run.result.model_usage[*].{model_key}",
                    sequences=sequences,
                    scope="whole_run_tree",
                    reason="aggregated token count exceeds the telemetry safety bound",
                )
            return _observed(
                total,
                unit="tokens",
                source=f"run.result.model_usage[*].{model_key}",
                sequences=sequences,
                scope="whole_run_tree",
            )
    if usage is not None and _valid_non_negative_int(usage.get(usage_key)):
        return _observed(
            int(usage[usage_key]),
            unit="tokens",
            source=f"run.result.usage.{usage_key}",
            sequences=sequences,
            scope="main_agent_only",
        )
    return _unavailable(
        unit="tokens",
        source=f"run.result.model_usage[*].{model_key}|usage.{usage_key}",
        sequences=sequences,
        reason="the SDK result did not record a valid token count",
    )


def _snapshot_string(run: RunRecord, key: str, *, maximum: int) -> str | None:
    snapshot = run.provider_snapshot
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get(key)
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= maximum
        else None
    )


def build_run_telemetry(
    run: RunRecord,
    events: list[EventEnvelope],
) -> RunTelemetry:
    """Aggregate one run from a complete durable event replay.

    The input may be delivered out of order; sequence numbers are authoritative and
    sorting is deterministic.  Gaps or duplicate sequence numbers make all "first"
    event milestones unavailable because their true ordering/completeness is unknown.
    """

    foreign = sorted({event.run_id for event in events if event.run_id != run.id})
    if foreign:
        raise ValueError(f"telemetry events do not belong to run {run.id}")

    warnings: list[TelemetryWarning] = []
    original_sequences = [event.sequence for event in events]
    ordered = sorted(events, key=lambda event: (event.sequence, event.id))
    sorted_sequences = [event.sequence for event in ordered]
    if original_sequences != sorted_sequences:
        warnings.append(
            TelemetryWarning(
                code="event_input_out_of_order",
                detail="Input events were reordered by their durable sequence numbers.",
                event_sequences=original_sequences,
            )
        )

    sequence_counts = Counter(sorted_sequences)
    duplicate_sequences = sorted(
        sequence for sequence, count in sequence_counts.items() if count > 1
    )
    if duplicate_sequences:
        warnings.append(
            TelemetryWarning(
                code="duplicate_event_sequence",
                detail="Duplicate sequence numbers make event ordering ambiguous.",
                event_sequences=duplicate_sequences,
            )
        )
    id_counts = Counter(event.id for event in ordered)
    duplicate_ids = {event_id for event_id, count in id_counts.items() if count > 1}
    if duplicate_ids:
        warnings.append(
            TelemetryWarning(
                code="duplicate_event_id",
                detail="Duplicate durable event ids make replay identity ambiguous.",
                event_sequences=[
                    event.sequence for event in ordered if event.id in duplicate_ids
                ],
            )
        )
    unique_sequences = sorted(sequence_counts)
    expected_sequences = list(range(1, unique_sequences[-1] + 1)) if unique_sequences else []
    missing_sequences = sorted(set(expected_sequences) - set(unique_sequences))
    if missing_sequences:
        warnings.append(
            TelemetryWarning(
                code="event_sequence_gap",
                detail="The supplied replay is missing durable event sequence numbers.",
                event_sequences=missing_sequences,
            )
        )
    event_log_trustworthy = (
        not duplicate_sequences and not duplicate_ids and not missing_sequences
    )
    event_log_integrity = _observed(
        event_log_trustworthy,
        unit="boolean",
        source="events[*].sequence|events[*].id",
        sequences=unique_sequences,
        scope="supplied_replay",
    )

    result_events = [event for event in ordered if event.type == EventType.RUN_RESULT.value]
    result_event = result_events[-1] if result_events else None
    if len(result_events) > 1 and not all(
        event.data.get("result_scope") == "run_cumulative" for event in result_events
    ):
        warnings.append(
            TelemetryWarning(
                code="multiple_run_results",
                detail=(
                    "Multiple SDK results were recorded; cumulative fields use the latest "
                    "result as required by streaming-input accounting semantics."
                ),
                event_sequences=[event.sequence for event in result_events],
            )
        )
    result_data = result_event.data if result_event is not None else {}
    result_metric_scope = (
        "run_cumulative" if result_data.get("result_scope") == "run_cumulative" else "sdk_query"
    )
    result_sequence = result_event.sequence if result_event is not None else None
    result_sequences = [result_sequence] if result_sequence is not None else []

    model_usage_raw = result_data.get("model_usage")
    model_usage = _bounded_model_usage(model_usage_raw)
    if model_usage_raw is not None and model_usage is None:
        warnings.append(
            TelemetryWarning(
                code="invalid_model_usage",
                detail=(
                    "run.result.model_usage exceeded bounds or did not match the SDK's "
                    "shallow scalar schema; it was not traversed."
                ),
                event_sequences=result_sequences,
            )
        )
    usage_raw = result_data.get("usage")
    usage = _bounded_usage(usage_raw)
    if usage_raw is not None and usage is None:
        warnings.append(
            TelemetryWarning(
                code="invalid_result_usage",
                detail="run.result.usage did not match the bounded scalar SDK schema.",
                event_sequences=result_sequences,
            )
        )

    subtype = result_data.get("subtype")
    crash_result = subtype == "error_during_execution"
    raw_accounting_values: list[object] = []
    if usage is not None:
        raw_accounting_values.extend(
            usage.get(key)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
    if model_usage is not None:
        for item in model_usage.values():
            raw_accounting_values.extend(
                item.get(key)
                for key in (
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                    "cacheCreationInputTokens",
                )
            )
    numeric_accounting = [
        value for value in raw_accounting_values if _valid_non_negative_int(value)
    ]
    crash_zeroed = crash_result and bool(numeric_accounting) and not any(numeric_accounting)
    if crash_zeroed:
        warnings.append(
            TelemetryWarning(
                code="sdk_crash_accounting_may_be_zeroed",
                detail=(
                    "The SDK documents that error_during_execution may zero usage and cost; "
                    "zeroes are reported as unavailable rather than real consumption."
                ),
                event_sequences=result_sequences,
            )
        )

    input_tokens = _usage_metric(
        model_usage=model_usage,
        usage=usage,
        model_key="inputTokens",
        usage_key="input_tokens",
        result_sequence=result_sequence,
        crash_zeroed=crash_zeroed,
    )
    output_tokens = _usage_metric(
        model_usage=model_usage,
        usage=usage,
        model_key="outputTokens",
        usage_key="output_tokens",
        result_sequence=result_sequence,
        crash_zeroed=crash_zeroed,
    )
    cache_read = _usage_metric(
        model_usage=model_usage,
        usage=usage,
        model_key="cacheReadInputTokens",
        usage_key="cache_read_input_tokens",
        result_sequence=result_sequence,
        crash_zeroed=crash_zeroed,
    )
    cache_creation = _usage_metric(
        model_usage=model_usage,
        usage=usage,
        model_key="cacheCreationInputTokens",
        usage_key="cache_creation_input_tokens",
        result_sequence=result_sequence,
        crash_zeroed=crash_zeroed,
    )
    if cache_read.availability == "observed" and isinstance(cache_read.value, int):
        cache_hit = _observed(
            cache_read.value > 0,
            unit="boolean",
            source=cache_read.source,
            sequences=cache_read.source_event_sequences,
            scope=cache_read.scope,
        )
    else:
        cache_hit = _unavailable(
            unit="boolean",
            source="tokens.cache_read_input_tokens",
            reason="cache-read tokens are unavailable",
            sequences=cache_read.source_event_sequences,
            scope=cache_read.scope,
        )

    total_cost = result_data.get("total_cost_usd")
    model_cost_values = (
        [item.get("costUSD") for item in model_usage.values()]
        if model_usage is not None
        else []
    )
    model_cost_total = (
        sum(float(value) for value in model_cost_values)
        if model_cost_values and all(_valid_cost(value) for value in model_cost_values)
        else None
    )
    valid_model_cost = model_cost_total is not None and model_cost_total <= MAX_COST_USD
    if crash_result and total_cost == 0 and not (
        valid_model_cost and model_cost_total is not None and model_cost_total > 0
    ):
        estimated_cost = _unavailable(
            unit="USD",
            source="run.result.total_cost_usd",
            sequences=result_sequences,
            scope="whole_run_tree",
            reason="SDK error_during_execution may report a zeroed cost estimate",
        )
    elif _valid_cost(total_cost):
        estimated_cost = _observed(
            float(total_cost),
            unit="USD",
            source="run.result.total_cost_usd",
            sequences=result_sequences,
            scope="whole_run_tree_estimate",
        )
    elif valid_model_cost:
        assert model_cost_total is not None
        estimated_cost = _observed(
            model_cost_total,
            unit="USD",
            source="run.result.model_usage[*].costUSD",
            sequences=result_sequences,
            scope="whole_run_tree_estimate",
        )
    else:
        estimated_cost = _unavailable(
            unit="USD",
            source="run.result.total_cost_usd|model_usage[*].costUSD",
            sequences=result_sequences,
            scope="whole_run_tree_estimate",
            reason="the SDK result did not record a valid cost estimate",
        )

    cost_basis_value = result_data.get("cost_basis")
    if isinstance(cost_basis_value, str) and cost_basis_value.strip():
        cost_basis = _observed(
            cost_basis_value,
            unit="label",
            source="run.result.cost_basis",
            sequences=result_sequences,
        )
    else:
        cost_basis = _unavailable(
            unit="label",
            source="run.result.cost_basis",
            sequences=result_sequences,
            reason="cost basis was not recorded",
        )

    tool_events: dict[str, list[EventEnvelope]] = defaultdict(list)
    unattributed: list[int] = []
    for event in ordered:
        if event.type not in _TOOL_LIFECYCLE_TYPES:
            continue
        if isinstance(event.tool_use_id, str) and event.tool_use_id:
            tool_events[event.tool_use_id].append(event)
        else:
            unattributed.append(event.sequence)
    if unattributed:
        warnings.append(
            TelemetryWarning(
                code="unattributed_tool_events",
                detail=(
                    "Tool lifecycle events without tool_use_id were not heuristically paired."
                ),
                event_sequences=unattributed,
            )
        )
    timings = [
        _tool_timing(tool_use_id, lifecycle)
        for tool_use_id, lifecycle in tool_events.items()
    ]
    timings.sort(
        key=lambda timing: (
            min(
                [
                    *timing.planned_event_sequences,
                    *timing.started_event_sequences,
                    *timing.terminal_event_sequences,
                ]
                or [MAX_TOKEN_COUNT]
            ),
            timing.tool_use_id,
        )
    )

    first_token = _first_event(
        ordered,
        lambda event: event.type == EventType.ASSISTANT_TEXT_DELTA.value
        and (_text_field(event, "text") or "") != "",
    )
    first_visible = _first_event(
        ordered,
        lambda event: (
            event.type
            in {
                EventType.ASSISTANT_TEXT_DELTA.value,
                EventType.ASSISTANT_MESSAGE_COMPLETED.value,
            }
            and bool((_text_field(event, "text") or "").strip())
        )
        or (
            event.type == EventType.RUN_RESULT.value
            and bool((_text_field(event, "result") or "").strip())
        ),
    )
    first_progress = _first_event(ordered, lambda event: event.type in _PROGRESS_TYPES)
    first_card = _first_event(
        ordered,
        lambda event: event.type == EventType.UI_PRESENTATION.value
        and _is_valid_typed_card(event, run.project_id),
    )

    first_card_data = (
        first_card.data.get("presentation") if first_card is not None else None
    )
    first_card_component_value = (
        first_card_data.get("component") if isinstance(first_card_data, dict) else None
    )
    if isinstance(first_card_component_value, str) and first_card_component_value:
        first_card_component = _observed(
            first_card_component_value,
            unit="identifier",
            source="ui.presentation.data.presentation.component",
            sequences=[first_card.sequence] if first_card is not None else [],
        )
    else:
        first_card_component = _unavailable(
            unit="identifier",
            source="ui.presentation.data.presentation.component",
            reason=(
                "event log is incomplete or has duplicate sequence numbers"
                if not event_log_trustworthy
                else "no runtime-validated typed presentation was observed"
            ),
        )
    if not event_log_trustworthy and first_card_component.availability == "observed":
        first_card_component = _unavailable(
            unit="identifier",
            source="ui.presentation.data.presentation.component",
            reason="event log is incomplete or has duplicate sequence numbers",
        )

    sdk_duration_value = result_data.get("duration_ms")
    if _valid_non_negative_int(sdk_duration_value):
        sdk_duration = _observed(
            int(sdk_duration_value),
            unit="milliseconds",
            source="run.result.duration_ms",
            sequences=result_sequences,
            scope=result_metric_scope,
        )
    else:
        sdk_duration = _unavailable(
            unit="milliseconds",
            source="run.result.duration_ms",
            sequences=result_sequences,
            scope=result_metric_scope,
            reason="the SDK result did not record a valid duration",
        )
    sdk_api_duration_value = result_data.get("duration_api_ms")
    if _valid_non_negative_int(sdk_api_duration_value):
        sdk_api_duration = _observed(
            int(sdk_api_duration_value),
            unit="milliseconds",
            source="run.result.duration_api_ms",
            sequences=result_sequences,
            scope="provider_api",
        )
    else:
        sdk_api_duration = _unavailable(
            unit="milliseconds",
            source="run.result.duration_api_ms",
            sequences=result_sequences,
            scope="provider_api",
            reason="the SDK result did not record a valid API duration",
        )

    turns_value = result_data.get("num_turns")
    if _valid_non_negative_int(turns_value, maximum=10**9):
        turns = _observed(
            int(turns_value),
            unit="turns",
            source="run.result.num_turns",
            sequences=result_sequences,
            scope=result_metric_scope,
        )
    else:
        turns = _unavailable(
            unit="turns",
            source="run.result.num_turns",
            sequences=result_sequences,
            scope=result_metric_scope,
            reason="the SDK result did not record a valid turn count",
        )

    observed_model_values: set[str] = set(model_usage or {})
    observed_model_sequences: set[int] = set(result_sequences if model_usage else [])
    for event in ordered:
        if event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
            model = _text_field(event, "model")
            if model and len(model) <= MAX_MODEL_NAME_LENGTH:
                observed_model_values.add(model)
                observed_model_sequences.add(event.sequence)
        elif event.type == EventType.SDK_SYSTEM.value:
            sdk_data = event.data.get("data")
            model = sdk_data.get("model") if isinstance(sdk_data, dict) else None
            if isinstance(model, str) and model and len(model) <= MAX_MODEL_NAME_LENGTH:
                observed_model_values.add(model)
                observed_model_sequences.add(event.sequence)
    if observed_model_values:
        observed_models = _observed(
            sorted(observed_model_values),
            unit="model_ids",
            source="run.result.model_usage|assistant.message.completed|sdk.system.init",
            sequences=sorted(observed_model_sequences),
            scope="observed_sdk_events",
        )
    else:
        observed_models = _unavailable(
            unit="model_ids",
            source="run.result.model_usage|assistant.message.completed|sdk.system.init",
            reason="no served model identifier was observed",
        )

    provider_id = run.provider_profile_id
    provider_profile = (
        _observed(
            provider_id,
            unit="identifier",
            source="run.provider_profile_id",
            scope="immutable_run_configuration",
        )
        if isinstance(provider_id, str) and provider_id.strip()
        else _unavailable(
            unit="identifier",
            source="run.provider_profile_id",
            scope="immutable_run_configuration",
            reason="this run does not record a provider profile id",
        )
    )
    display_name = _snapshot_string(run, "display_name", maximum=160)
    provider_display_name = (
        _observed(
            display_name,
            unit="label",
            source="run.provider_snapshot.display_name",
            scope="immutable_run_configuration",
        )
        if display_name is not None
        else _unavailable(
            unit="label",
            source="run.provider_snapshot.display_name",
            scope="immutable_run_configuration",
            reason="the immutable provider snapshot has no display name",
        )
    )
    configured_model_value = _snapshot_string(run, "model", maximum=MAX_MODEL_NAME_LENGTH)
    configured_model = (
        _observed(
            configured_model_value,
            unit="model_id",
            source="run.provider_snapshot.model",
            scope="immutable_run_configuration",
        )
        if configured_model_value is not None
        else _unavailable(
            unit="model_id",
            source="run.provider_snapshot.model",
            scope="immutable_run_configuration",
            reason="the immutable run snapshot has no configured model",
        )
    )

    result_subtype = (
        _observed(
            subtype,
            unit="label",
            source="run.result.subtype",
            sequences=result_sequences,
        )
        if isinstance(subtype, str) and subtype
        else _unavailable(
            unit="label",
            source="run.result.subtype",
            sequences=result_sequences,
            reason="no SDK result subtype was recorded",
        )
    )
    is_error_value = result_data.get("is_error")
    result_is_error = (
        _observed(
            is_error_value,
            unit="boolean",
            source="run.result.is_error",
            sequences=result_sequences,
        )
        if isinstance(is_error_value, bool)
        else _unavailable(
            unit="boolean",
            source="run.result.is_error",
            sequences=result_sequences,
            reason="no SDK result error flag was recorded",
        )
    )

    return RunTelemetry(
        run_id=run.id,
        project_id=run.project_id,
        trace_id=run.trace_id,
        computed_through_sequence=max(unique_sequences, default=0),
        event_log_complete_and_unambiguous=event_log_integrity,
        prompt_prefix_hash=(
            _observed(
                run.context_refresh.prompt_prefix_hash,
                unit="sha256",
                source="run.context_refresh.prompt_prefix_hash",
                scope="exact_sdk_system_prompt_append_utf8",
            )
            if run.context_refresh is not None
            and run.context_refresh.prompt_prefix_hash is not None
            else _unavailable(
                unit="sha256",
                source="run.context_refresh.prompt_prefix_hash",
                reason="the run predates durable prompt-prefix hash capture",
            )
        ),
        latency=RunLatencyTelemetry(
            run_queue_duration_ms=_duration_between(
                run.created_at,
                run.started_at,
                source="run.created_at→run.started_at",
                missing_reason="run.started_at is unavailable",
            ),
            total_task_duration_ms=(
                _duration_between(
                    run.started_at,
                    run.finished_at,
                    source="run.started_at→run.finished_at",
                    missing_reason="run start or finish timestamp is unavailable",
                )
                if run.status in TERMINAL_RUN_STATUSES
                else _unavailable(
                    unit="milliseconds",
                    source="run.started_at→run.finished_at",
                    reason="run has not reached a terminal status",
                )
            ),
            sdk_duration_ms=sdk_duration,
            sdk_api_duration_ms=sdk_api_duration,
            ttft_ms=_event_metric(
                first_token,
                run.started_at,
                source="run.started_at→first non-empty assistant.text.delta",
                missing_reason="no streamed assistant text token was observed",
                log_trustworthy=event_log_trustworthy,
            ),
            first_visible_text_ms=_event_metric(
                first_visible,
                run.started_at,
                source=(
                    "run.started_at→first visible assistant text/result event"
                ),
                missing_reason="no user-visible assistant text was observed",
                log_trustworthy=event_log_trustworthy,
            ),
            first_progress_ms=_event_metric(
                first_progress,
                run.started_at,
                source="run.started_at→first tool/task/subagent lifecycle event",
                missing_reason="no progress lifecycle event was observed",
                log_trustworthy=event_log_trustworthy,
            ),
            first_useful_typed_card_ms=_event_metric(
                first_card,
                run.started_at,
                source="run.started_at→first runtime-validated ui.presentation",
                missing_reason="no runtime-validated typed presentation was observed",
                log_trustworthy=event_log_trustworthy,
            ),
            first_useful_typed_card_component=first_card_component,
        ),
        tools=ToolTelemetry(
            timings=timings,
            unattributed_event_sequences=unattributed,
            queue_durations=_duration_aggregate(timings, "queue_duration_ms"),
            run_durations=_duration_aggregate(timings, "run_duration_ms"),
        ),
        tokens=TokenTelemetry(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            cache_hit=cache_hit,
        ),
        cost=CostTelemetry(
            estimated_cost_usd=estimated_cost,
            cost_basis=cost_basis,
        ),
        provider=ProviderTelemetry(
            provider_profile_id=provider_profile,
            provider_display_name=provider_display_name,
            configured_model=configured_model,
            observed_models=observed_models,
        ),
        outcome=OutcomeTelemetry(
            run_status=run.status,
            completed=_observed(
                run.status == RunStatus.COMPLETED,
                unit="boolean",
                source="run.status",
            ),
            result_subtype=result_subtype,
            result_is_error=result_is_error,
            turn_count=turns,
        ),
        quality_warnings=warnings,
    )
