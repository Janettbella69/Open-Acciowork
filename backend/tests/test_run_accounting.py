"""A resumed Run must never present a partial SDK segment total as complete usage."""

import pytest

from ophira_backend.constants import EventType
from ophira_backend.models import EventActor, EventEnvelope, ProjectCreate
from ophira_backend.run_accounting import cumulative_result
from ophira_backend.storage import FileLedger
from ophira_backend.telemetry import build_run_telemetry


def segment(**changes):
    return {
        "subtype": "success", "total_cost_usd": 0.4, "num_turns": 2,
        "duration_ms": 10, "duration_api_ms": 8,
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "model_usage": {"model-a": {
            "inputTokens": 20, "outputTokens": 10, "costUSD": 0.4,
            "contextWindow": 200000,
        }},
        **changes,
    }


@pytest.mark.parametrize("field", ["usage", "model_usage"])
@pytest.mark.parametrize("missing_index", [0, 1])
def test_missing_segment_accounting_stays_unknown_after_later_results(field, missing_index):
    result = None
    for index in range(3):
        current = segment(**({field: None} if index == missing_index else {}))
        result = cumulative_result(result, current)
    assert result[field] is None
    assert result["total_cost_usd"] == pytest.approx(1.2)
    assert result[f"segment_{field}"] == segment()[field]


@pytest.mark.parametrize("missing_index", [0, 1])
def test_missing_usage_counter_is_not_assumed_to_be_zero(missing_index):
    result = None
    for index in range(3):
        usage = {"input_tokens": 20}
        if index != missing_index:
            usage["output_tokens"] = 10
        result = cumulative_result(result, segment(usage=usage))
    assert result["usage"] == {"input_tokens": 60, "output_tokens": None}


def test_zeroed_crash_segment_cannot_hide_spend_behind_previous_success():
    before = cumulative_result(None, segment())
    crashed = cumulative_result(before, segment(
        subtype="error_during_execution", total_cost_usd=0,
        usage={"input_tokens": 0, "output_tokens": 0},
        model_usage={"model-a": {"inputTokens": 0, "outputTokens": 0, "costUSD": 0}},
    ))
    assert crashed["total_cost_usd"] is None
    assert crashed["usage"] is None
    assert crashed["model_usage"] is None
    assert crashed["segment_total_cost_usd"] == 0
    resumed = cumulative_result(crashed, segment())
    assert resumed["total_cost_usd"] is None
    assert resumed["usage"] is None
    assert resumed["model_usage"] is None


def test_model_switch_keeps_complete_per_model_totals_and_latest_metadata():
    before = cumulative_result(None, segment())
    switched = segment(model_usage={"model-b": {
        "inputTokens": 5, "outputTokens": 3, "costUSD": 0.1, "contextWindow": 100000,
    }}, total_cost_usd=0.1)
    result = cumulative_result(before, switched)
    assert result["model_usage"]["model-a"] == before["model_usage"]["model-a"]
    assert result["model_usage"]["model-b"] == switched["model_usage"]["model-b"]


def test_finite_segment_values_cannot_overflow_into_nonfinite_totals():
    first = cumulative_result(None, segment(total_cost_usd=1e308))
    result = cumulative_result(first, segment(total_cost_usd=1e308))
    assert result["total_cost_usd"] is None


@pytest.mark.parametrize("invalid", [True, -1, float("inf"), float("nan"), 10**1000])
def test_invalid_numeric_accounting_is_unavailable(invalid):
    result = cumulative_result(None, segment(total_cost_usd=invalid))
    assert result["total_cost_usd"] is None


def test_telemetry_does_not_present_partial_segment_accounting_as_the_whole_run(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Accounting", objective="Track the run"))
    run = ledger.create_run(project.id, "Observe usage")
    result = None
    for index, current in enumerate([
        segment(), segment(total_cost_usd=None, usage=None, model_usage=None),
    ], start=1):
        result = cumulative_result(result, current)
        ledger.append_event(EventEnvelope(
            id="pending", sequence=index, type=EventType.RUN_RESULT.value,
            project_id=project.id, run_id=run.id, trace_id=run.trace_id,
            actor=EventActor(type="agent", name="main"), data=result,
        ))
    telemetry = build_run_telemetry(run, ledger.list_events(run.id))
    assert telemetry.cost.estimated_cost_usd.availability == "unavailable"
    assert telemetry.tokens.input_tokens.availability == "unavailable"
