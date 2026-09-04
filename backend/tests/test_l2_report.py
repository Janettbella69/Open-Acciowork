from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_l2.py"
SPEC = importlib.util.spec_from_file_location("ophira_run_l2", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_release_aggregate_reports_observed_distributions_and_cost_per_success() -> None:
    aggregate = MODULE.aggregate_results(
        [
            {
                "passed": True,
                "total_task_duration_ms": 100,
                "ttft_ms": 20,
                "first_useful_typed_card_ms": 50,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "cache_hit": True,
                "observed_cost_usd": 0.20,
            },
            {
                "passed": False,
                "total_task_duration_ms": 300,
                "ttft_ms": 60,
                "first_useful_typed_card_ms": None,
                "input_tokens": 20,
                "output_tokens": 8,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 4,
                "cache_hit": False,
                "observed_cost_usd": 0.40,
            },
            {"passed": True, "observed_cost_usd": 0.10},
        ]
    )

    assert aggregate["pass_rate"] == 2 / 3
    assert aggregate["latency_ms"]["total_task"] == {
        "observed_runs": 2,
        "p50": 200.0,
        "p99": 298.0,
    }
    assert aggregate["latency_ms"]["first_useful_card"]["observed_runs"] == 1
    assert aggregate["tokens"] == {
        "observed_runs": 2,
        "input_total": 30.0,
        "output_total": 13.0,
        "cache_read_total": 3.0,
        "cache_creation_total": 6.0,
    }
    assert aggregate["cache_hit"] == {
        "observed_runs": 2,
        "hit_runs": 1,
        "hit_rate": 0.5,
    }
    assert aggregate["cost"] == {
        "observed_runs": 3,
        "capture_complete": True,
        "observed_total_usd": pytest.approx(0.7),
        "total_usd": pytest.approx(0.7),
        "cost_per_success_usd": pytest.approx(0.35),
    }


def test_release_aggregate_preserves_unavailable_metrics_as_null() -> None:
    aggregate = MODULE.aggregate_results([{"passed": False, "error": "startup"}])

    assert aggregate["pass_rate"] == 0
    assert aggregate["latency_ms"]["ttft"] == {
        "observed_runs": 0,
        "p50": None,
        "p99": None,
    }
    assert aggregate["tokens"]["input_total"] is None
    assert aggregate["cache_hit"] == {
        "observed_runs": 0,
        "hit_runs": 0,
        "hit_rate": None,
    }
    assert aggregate["cost"]["capture_complete"] is False
    assert aggregate["cost"]["observed_total_usd"] is None
    assert aggregate["cost"]["total_usd"] is None
    assert aggregate["cost"]["cost_per_success_usd"] is None


def test_release_cost_per_success_is_unavailable_when_any_run_cost_is_missing() -> None:
    aggregate = MODULE.aggregate_results(
        [
            {"passed": True, "observed_cost_usd": 0.25},
            {"passed": False, "observed_cost_usd": None},
        ]
    )

    assert aggregate["cost"] == {
        "observed_runs": 1,
        "capture_complete": False,
        "observed_total_usd": 0.25,
        "total_usd": None,
        "cost_per_success_usd": None,
    }


def test_provider_invocation_requires_a_typed_sdk_result_event() -> None:
    noisy_initialization = [
        SimpleNamespace(type="sdk.system", data={"subtype": "init"}),
        SimpleNamespace(type="assistant.message.started", data={"message": {}}),
        SimpleNamespace(type="assistant.message.completed", data={"text": "hello"}),
        SimpleNamespace(type="run.result", data={"subtype": "success"}),
    ]

    assert MODULE._provider_was_invoked(noisy_initialization) is False
    assert MODULE._provider_was_invoked(
        [
            *noisy_initialization,
            SimpleNamespace(
                type="run.result",
                data={"subtype": "success", "is_error": False},
            ),
        ]
    ) is True


def test_receipt_capture_failure_is_distinct_from_an_observed_empty_ledger() -> None:
    working = SimpleNamespace(
        list_tool_receipts=lambda _project_id, *, run_id: [],
        replay_tool_receipt=lambda _project_id, _receipt_id: None,
    )
    broken = SimpleNamespace(
        list_tool_receipts=lambda _project_id, *, run_id: (_ for _ in ()).throw(
            OSError("ledger unavailable")
        ),
        replay_tool_receipt=lambda _project_id, _receipt_id: None,
    )

    assert MODULE._receipt_snapshots(working, "project", "run") == ([], True)
    assert MODULE._receipt_snapshots(broken, "project", "run") == ([], False)
