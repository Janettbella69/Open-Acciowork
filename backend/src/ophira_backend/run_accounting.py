"""Accumulate native SDK subprocess segments inside one logical Run."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .constants import EventType
from .storage import FileLedger

MODEL_TOTAL_KEYS = {
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "webSearchRequests",
    "costUSD",
}
_FIRST_SEGMENT = object()


@dataclass(frozen=True)
class SdkInvocationAccounting:
    """Separate per-turn counters from one CLI process's running estimates.

    Native streaming results report turn-local ``usage`` but process-cumulative
    ``total_cost_usd`` and ``model_usage``. A new CLI, including durable resume,
    starts those estimates over. Freeze the earlier invocations at connection
    time so later results replace this invocation's estimate instead of adding
    its already reported spend again.
    """

    previous_invocations: dict[str, Any] | None

    def accumulate(self, previous: dict | None, current: dict) -> dict:
        result = cumulative_result(previous, current)
        invocation = cumulative_result(self.previous_invocations, current)
        result["total_cost_usd"] = invocation["total_cost_usd"]
        result["model_usage"] = invocation["model_usage"]
        return result


def latest_result(ledger: FileLedger, run_id: str) -> dict[str, Any] | None:
    return next(
        (
            event.data
            for event in reversed(ledger.list_events(run_id))
            if event.type == EventType.RUN_RESULT.value
        ),
        None,
    )


def _sum(left: Any, right: Any) -> int | float | None:
    if not _valid_number(left) or not _valid_number(right):
        return None
    total = left + right
    return total if _valid_number(total) else None


def _valid_number(value: Any) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _usage(previous: Any, current: Any) -> dict | None:
    """Missing counters are unknown, including when later segments report them."""
    first = previous is _FIRST_SEGMENT
    if not isinstance(current, dict) or not (first or isinstance(previous, dict)):
        return None
    previous = {} if first else previous
    result = {}
    for key in previous.keys() | current.keys():
        value = current.get(key)
        if isinstance(value, dict):
            result[key] = _usage(_FIRST_SEGMENT if first else previous.get(key), value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = _sum(0 if first else previous.get(key), value)
        else:
            result[key] = value
    return result


def _models(previous: Any, current: Any) -> dict | None:
    first = previous is _FIRST_SEGMENT
    if (not isinstance(current, dict)
            or not all(isinstance(value, dict) for value in current.values())
            or not (first or isinstance(previous, dict))):
        return None
    previous = {} if first else previous
    if not all(isinstance(value, dict) for value in previous.values()):
        return None
    models = {}
    for model in previous.keys() | current.keys():
        old, new = previous.get(model, {}), current.get(model, {})
        combined = {**old, **new}
        for key in MODEL_TOTAL_KEYS & combined.keys():
            # A model absent from a complete segment map did no work there;
            # a model present with a missing counter did not report that value.
            combined[key] = _sum(
                old.get(key) if model in previous else 0,
                new.get(key) if model in current else 0,
            )
        models[model] = combined
    return models or None


def _has_positive_counter(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_positive_counter(item) for item in value.values())
    return _valid_number(value) and value > 0


def cumulative_result(previous: dict | None, current: dict) -> dict:
    first = previous is None
    previous = previous or {}
    result = dict(current)
    result.update(
        result_scope="run_cumulative",
        segment_total_cost_usd=current.get("total_cost_usd"),
        segment_num_turns=current.get("num_turns"),
        segment_usage=current.get("usage"),
        segment_model_usage=current.get("model_usage"),
    )
    # A fatal SDK crash can zero this subprocess's accounting. Adding those
    # zeros to previous spend would disguise missing consumption as complete.
    crash = current.get("subtype") == "error_during_execution"
    for key in ("total_cost_usd", "num_turns", "duration_ms", "duration_api_ms"):
        value = current.get(key)
        if crash and key == "total_cost_usd" and value == 0:
            value = None
        result[key] = _sum(0 if first else previous.get(key), value)
    usage = current.get("usage")
    if crash and not _has_positive_counter(usage):
        usage = None
    result["usage"] = _usage(_FIRST_SEGMENT if first else previous.get("usage"), usage)
    models = current.get("model_usage")
    counters = (
        [{key: value for key, value in item.items() if key in MODEL_TOTAL_KEYS}
         for item in models.values() if isinstance(item, dict)]
        if isinstance(models, dict) else []
    )
    if crash and not any(_has_positive_counter(item) for item in counters):
        models = None
    result["model_usage"] = _models(
        _FIRST_SEGMENT if first else previous.get("model_usage"), models,
    )
    return result
