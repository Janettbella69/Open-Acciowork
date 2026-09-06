from datetime import UTC, datetime, timedelta

import pytest

from ophira_backend.constants import EventType, RunStatus
from ophira_backend.models import (
    ContextRefreshRecord,
    ContextRevisionManifest,
    EventActor,
    EventEnvelope,
    RunRecord,
)
from ophira_backend.telemetry import build_run_telemetry
from ophira_backend.untrusted import UNTRUSTED_DATA_NOTICE

BASE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def make_run(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    started_at: datetime | None = BASE + timedelta(seconds=1),
    finished_at: datetime | None = BASE + timedelta(seconds=11, milliseconds=500),
) -> RunRecord:
    return RunRecord(
        id="run_telemetry",
        trace_id="trace_telemetry",
        project_id="project_telemetry",
        prompt="Measure the durable run",
        provider_profile_id="provider_a",
        provider_snapshot={
            "id": "provider_a",
            "display_name": "Provider A",
            "model": "configured-model",
        },
        status=status,
        created_at=BASE,
        started_at=started_at,
        finished_at=finished_at,
    )


def event(
    run: RunRecord,
    sequence: int,
    event_type: EventType,
    offset_ms: int,
    data: dict | None = None,
    *,
    tool_use_id: str | None = None,
    event_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        id=event_id or f"{run.id}:{sequence}",
        sequence=sequence,
        type=event_type.value,
        run_id=run.id,
        project_id=run.project_id,
        trace_id=run.trace_id,
        timestamp=BASE + timedelta(milliseconds=offset_ms),
        actor=EventActor(type="agent", name="ophira-main"),
        tool_use_id=tool_use_id,
        data=data or {},
    )


def artifact_presentation(run: RunRecord) -> dict:
    artifact_id = "artifact_telemetry"
    return {
        "presentation": {
            "external_data_notice": UNTRUSTED_DATA_NOTICE,
            "external_data_truncated": False,
            "schema_version": "1",
            "presentation_id": "view_telemetry",
            "component": "artifact_list",
            "project_id": run.project_id,
            "project_revision": 1,
            "origin": "project_ledger",
            "payload": {
                "title": "Generated artifact",
                "items": [
                    {
                        "id": artifact_id,
                        "name": "report.md",
                        "mime": "text/markdown",
                        "version": 1,
                        "size_bytes": 42,
                        "download_url": (
                            f"/api/projects/{run.project_id}/artifacts/"
                            f"{artifact_id}/download"
                        ),
                        "source_ids": [],
                        "run_id": run.id,
                        "updated_at": "2026-09-02T12:00:05Z",
                    }
                ],
            },
        }
    }


def test_complete_replay_aggregates_latency_tools_usage_cost_and_provider() -> None:
    run = make_run()
    events = [
        event(run, 1, EventType.RUN_STARTED, 1_000),
        event(
            run,
            2,
            EventType.TASK_PROGRESS,
            1_100,
            {"description": "Reading sources"},
        ),
        event(
            run,
            3,
            EventType.ASSISTANT_TEXT_DELTA,
            1_250,
            {"text": "First token"},
        ),
        event(
            run,
            4,
            EventType.TOOL_PLANNED,
            2_000,
            {"tool_name": "WebSearch"},
            tool_use_id="tool_1",
        ),
        event(
            run,
            5,
            EventType.TOOL_STARTED,
            2_125,
            {"tool_name": "WebSearch"},
            tool_use_id="tool_1",
        ),
        event(
            run,
            6,
            EventType.TOOL_COMPLETED,
            4_500,
            {"tool_name": "WebSearch"},
            tool_use_id="tool_1",
        ),
        event(
            run,
            7,
            EventType.UI_PRESENTATION,
            5_000,
            artifact_presentation(run),
            tool_use_id="tool_1",
        ),
        event(
            run,
            8,
            EventType.ASSISTANT_MESSAGE_COMPLETED,
            6_000,
            {"text": "Finished", "model": "served-main-model"},
        ),
        event(
            run,
            9,
            EventType.RUN_RESULT,
            10_000,
            {
                "subtype": "success",
                "is_error": False,
                "duration_ms": 9_000,
                "duration_api_ms": 7_500,
                "num_turns": 4,
                "total_cost_usd": 0.12,
                "cost_basis": "sdk_anthropic_list_price",
                "usage": {"input_tokens": 999, "output_tokens": 999},
                "model_usage": {
                    "served-main-model": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "cacheReadInputTokens": 40,
                        "cacheCreationInputTokens": 10,
                        "costUSD": 0.1,
                    },
                    "served-subagent-model": {
                        "inputTokens": 50,
                        "outputTokens": 5,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 3,
                        "costUSD": 0.02,
                    },
                },
            },
        ),
        event(run, 10, EventType.RUN_COMPLETED, 11_500),
    ]

    telemetry = build_run_telemetry(run, events)

    assert telemetry.schema_version == "1"
    assert telemetry.computed_through_sequence == 10
    assert telemetry.event_log_complete_and_unambiguous.value is True
    assert telemetry.prompt_prefix_hash.value is None
    assert "predates durable" in (
        telemetry.prompt_prefix_hash.unavailable_reason or ""
    )
    assert telemetry.quality_warnings == []
    assert telemetry.latency.run_queue_duration_ms.value == 1_000
    assert telemetry.latency.total_task_duration_ms.value == 10_500
    assert telemetry.latency.sdk_duration_ms.value == 9_000
    assert telemetry.latency.sdk_api_duration_ms.value == 7_500
    assert telemetry.latency.ttft_ms.value == 250
    assert telemetry.latency.first_visible_text_ms.value == 250
    assert telemetry.latency.first_progress_ms.value == 100
    assert telemetry.latency.first_useful_typed_card_ms.value == 4_000
    assert telemetry.latency.first_useful_typed_card_component.value == "artifact_list"

    assert len(telemetry.tools.timings) == 1
    timing = telemetry.tools.timings[0]
    assert timing.lifecycle_status == "completed"
    assert timing.tool_name.value == "WebSearch"
    assert timing.queue_duration_ms.value == 125
    assert timing.run_duration_ms.value == 2_375
    assert telemetry.tools.queue_durations.observed_total_ms.value == 125
    assert telemetry.tools.run_durations.observed_total_ms.value == 2_375

    assert telemetry.tokens.input_tokens.value == 150
    assert telemetry.tokens.output_tokens.value == 25
    assert telemetry.tokens.cache_read_input_tokens.value == 40
    assert telemetry.tokens.cache_creation_input_tokens.value == 13
    assert telemetry.tokens.input_tokens.scope == "whole_run_tree"
    assert telemetry.tokens.cache_hit.value is True
    assert telemetry.cost.estimated_cost_usd.value == pytest.approx(0.12)
    assert telemetry.cost.cost_basis.value == "sdk_anthropic_list_price"
    assert telemetry.provider.provider_profile_id.value == "provider_a"
    assert telemetry.provider.provider_display_name.value == "Provider A"
    assert telemetry.provider.configured_model.value == "configured-model"
    assert telemetry.provider.observed_models.value == [
        "served-main-model",
        "served-subagent-model",
    ]
    assert telemetry.outcome.completed.value is True
    assert telemetry.outcome.turn_count.value == 4


def test_prompt_prefix_hash_comes_from_the_durable_context_receipt() -> None:
    digest = "a" * 64
    manifest = ContextRevisionManifest(
        project_id="project_telemetry",
        project_revision=1,
        project_section_hashes={},
        agent_core_hash="b" * 64,
        memory_revision="c" * 64,
        skill_snapshot_hash="d" * 64,
        manifest_hash="e" * 64,
    )
    run = make_run().model_copy(
        update={
            "context_refresh": ContextRefreshRecord(
                mode="full",
                fallback_reason="new_session",
                changed_domains=["project"],
                changed_project_sections=["brief"],
                manifest=manifest,
                payload_hash="f" * 64,
                prompt_prefix_hash=digest,
            )
        }
    )

    telemetry = build_run_telemetry(run, [])

    assert telemetry.prompt_prefix_hash.value == digest
    assert telemetry.prompt_prefix_hash.availability == "observed"
    assert telemetry.prompt_prefix_hash.source == (
        "run.context_refresh.prompt_prefix_hash"
    )


def test_absent_observations_are_null_with_explicit_reasons() -> None:
    run = make_run(
        status=RunStatus.QUEUED,
        started_at=None,
        finished_at=None,
    ).model_copy(
        update={
            "provider_profile_id": None,
            "provider_snapshot": None,
        }
    )

    telemetry = build_run_telemetry(run, [])

    assert telemetry.event_log_complete_and_unambiguous.value is True
    assert telemetry.latency.total_task_duration_ms.value is None
    assert telemetry.latency.total_task_duration_ms.availability == "unavailable"
    assert telemetry.latency.total_task_duration_ms.unavailable_reason
    assert telemetry.latency.ttft_ms.value is None
    assert telemetry.tokens.input_tokens.value is None
    assert telemetry.tokens.cache_read_input_tokens.value is None
    assert telemetry.tokens.cache_hit.value is None
    assert telemetry.cost.estimated_cost_usd.value is None
    assert telemetry.provider.provider_profile_id.value is None
    assert telemetry.provider.configured_model.value is None
    assert telemetry.provider.observed_models.value is None
    assert telemetry.outcome.completed.value is False
    assert telemetry.outcome.turn_count.value is None


def test_main_agent_usage_is_labeled_and_missing_cache_write_is_not_zero() -> None:
    run = make_run()
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.RUN_RESULT,
                10_000,
                {
                    "subtype": "success",
                    "is_error": False,
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 0,
                    },
                },
            )
        ],
    )

    assert telemetry.tokens.input_tokens.value == 12
    assert telemetry.tokens.input_tokens.scope == "main_agent_only"
    assert telemetry.tokens.output_tokens.value == 4
    assert telemetry.tokens.cache_read_input_tokens.value == 0
    assert telemetry.tokens.cache_hit.value is False
    assert telemetry.tokens.cache_creation_input_tokens.value is None
    assert telemetry.tokens.cache_creation_input_tokens.availability == "unavailable"


def test_malformed_model_usage_is_bounded_without_recursive_traversal() -> None:
    run = make_run()
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.RUN_RESULT,
                10_000,
                {
                    "subtype": "success",
                    "is_error": False,
                    "model_usage": {
                        "malformed-model": {
                            "inputTokens": 900,
                            "unexpected_nested": {"do_not": ["traverse"]},
                        }
                    },
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            )
        ],
    )

    assert telemetry.tokens.input_tokens.value == 7
    assert telemetry.tokens.input_tokens.scope == "main_agent_only"
    assert telemetry.provider.observed_models.value is None
    assert "invalid_model_usage" in {
        warning.code for warning in telemetry.quality_warnings
    }


def test_model_usage_aggregate_cannot_exceed_token_safety_bound() -> None:
    run = make_run()
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.RUN_RESULT,
                10_000,
                {
                    "subtype": "success",
                    "is_error": False,
                    "model_usage": {
                        "model-a": {"inputTokens": 10**15},
                        "model-b": {"inputTokens": 10**15},
                    },
                },
            )
        ],
    )

    assert telemetry.tokens.input_tokens.value is None
    assert "safety bound" in (
        telemetry.tokens.input_tokens.unavailable_reason or ""
    )


def test_sdk_crash_zeroes_are_unavailable_not_claimed_as_free() -> None:
    run = make_run(status=RunStatus.FAILED)
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.RUN_RESULT,
                9_000,
                {
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "num_turns": 0,
                    "total_cost_usd": 0,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            )
        ],
    )

    assert telemetry.tokens.input_tokens.value is None
    assert telemetry.tokens.output_tokens.value is None
    assert telemetry.tokens.cache_read_input_tokens.value is None
    assert telemetry.cost.estimated_cost_usd.value is None
    assert telemetry.outcome.turn_count.value == 0
    assert "sdk_crash_accounting_may_be_zeroed" in {
        warning.code for warning in telemetry.quality_warnings
    }


def test_tool_pairing_refuses_out_of_order_duplicates_and_missing_ids() -> None:
    run = make_run()
    events = [
        event(
            run,
            1,
            EventType.TOOL_STARTED,
            3_000,
            {"tool_name": "Read"},
            tool_use_id="tool_out_of_order",
        ),
        event(
            run,
            2,
            EventType.TOOL_PLANNED,
            2_000,
            {"tool_name": "Read"},
            tool_use_id="tool_out_of_order",
        ),
        event(
            run,
            3,
            EventType.TOOL_COMPLETED,
            2_500,
            {"tool_name": "Read"},
            tool_use_id="tool_out_of_order",
        ),
        event(
            run,
            4,
            EventType.TOOL_PLANNED,
            4_000,
            {"tool_name": "WebFetch"},
            tool_use_id="tool_duplicate",
        ),
        event(
            run,
            5,
            EventType.TOOL_PLANNED,
            4_100,
            {"tool_name": "WebFetch"},
            tool_use_id="tool_duplicate",
        ),
        event(
            run,
            6,
            EventType.TOOL_STARTED,
            4_200,
            {"tool_name": "WebFetch"},
            tool_use_id="tool_duplicate",
        ),
        event(
            run,
            7,
            EventType.TOOL_COMPLETED,
            4_500,
            {"tool_name": "WebFetch"},
            tool_use_id="tool_duplicate",
        ),
        event(
            run,
            8,
            EventType.TOOL_STARTED,
            5_000,
            {"tool_name": "Bash"},
        ),
    ]

    telemetry = build_run_telemetry(run, events)

    first, second = telemetry.tools.timings
    assert first.tool_use_id == "tool_out_of_order"
    assert first.queue_duration_ms.value is None
    assert first.run_duration_ms.value is None
    assert "tool_lifecycle_out_of_order" in first.quality_warnings
    assert second.lifecycle_status == "ambiguous"
    assert second.queue_duration_ms.value is None
    assert "duplicate_tool_planned" in second.quality_warnings
    assert second.run_duration_ms.value == 300
    assert telemetry.tools.unattributed_event_sequences == [8]
    assert "unattributed_tool_events" in {
        warning.code for warning in telemetry.quality_warnings
    }


def test_duplicate_sequences_make_first_milestones_unavailable() -> None:
    run = make_run()
    events = [
        event(
            run,
            1,
            EventType.ASSISTANT_TEXT_DELTA,
            1_100,
            {"text": "visible"},
        ),
        event(
            run,
            1,
            EventType.TASK_PROGRESS,
            1_050,
            {"description": "working"},
            event_id=f"{run.id}:duplicate-1",
        ),
    ]

    telemetry = build_run_telemetry(run, events)

    assert telemetry.event_log_complete_and_unambiguous.value is False
    assert telemetry.latency.ttft_ms.value is None
    assert telemetry.latency.first_visible_text_ms.value is None
    assert telemetry.latency.first_progress_ms.value is None
    assert "duplicate_event_sequence" in {
        warning.code for warning in telemetry.quality_warnings
    }


def test_duplicate_event_ids_make_first_milestones_unavailable() -> None:
    run = make_run()
    duplicate_id = f"{run.id}:duplicate"
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.ASSISTANT_TEXT_DELTA,
                1_100,
                {"text": "visible"},
                event_id=duplicate_id,
            ),
            event(
                run,
                2,
                EventType.TASK_PROGRESS,
                1_200,
                {"description": "working"},
                event_id=duplicate_id,
            ),
        ],
    )

    assert telemetry.event_log_complete_and_unambiguous.value is False
    assert telemetry.latency.ttft_ms.value is None
    assert telemetry.latency.first_progress_ms.value is None
    assert "duplicate_event_id" in {
        warning.code for warning in telemetry.quality_warnings
    }


def test_completed_text_is_visible_fallback_but_not_a_streamed_ttft() -> None:
    run = make_run()
    malformed_card = {"presentation": {"schema_version": "1"}}
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.UI_PRESENTATION,
                2_000,
                malformed_card,
            ),
            event(
                run,
                2,
                EventType.ASSISTANT_MESSAGE_COMPLETED,
                3_000,
                {"text": "Visible only when complete", "model": "served-model"},
            ),
        ],
    )

    assert telemetry.latency.ttft_ms.value is None
    assert telemetry.latency.first_visible_text_ms.value == 2_000
    assert telemetry.latency.first_useful_typed_card_ms.value is None
    assert telemetry.latency.first_useful_typed_card_component.value is None
    assert telemetry.provider.observed_models.value == ["served-model"]


def test_assistant_usage_without_result_is_not_summed_or_invented() -> None:
    run = make_run()
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.ASSISTANT_MESSAGE_COMPLETED,
                3_000,
                {
                    "text": "Partial work",
                    "model": "served-model",
                    "usage": {"input_tokens": 1_000, "output_tokens": 1},
                },
            )
        ],
    )

    assert telemetry.tokens.input_tokens.value is None
    assert telemetry.tokens.output_tokens.value is None
    assert telemetry.cost.estimated_cost_usd.value is None


def test_out_of_order_input_is_sorted_and_warned() -> None:
    run = make_run()
    events = [
        event(
            run,
            2,
            EventType.TOOL_STARTED,
            2_000,
            {"tool_name": "Read"},
            tool_use_id="tool_1",
        ),
        event(
            run,
            1,
            EventType.TOOL_PLANNED,
            1_500,
            {"tool_name": "Read"},
            tool_use_id="tool_1",
        ),
        event(
            run,
            3,
            EventType.TOOL_COMPLETED,
            2_500,
            {"tool_name": "Read"},
            tool_use_id="tool_1",
        ),
    ]

    telemetry = build_run_telemetry(run, events)

    assert telemetry.event_log_complete_and_unambiguous.value is True
    assert telemetry.tools.timings[0].queue_duration_ms.value == 500
    assert telemetry.tools.timings[0].run_duration_ms.value == 500
    assert telemetry.quality_warnings[0].code == "event_input_out_of_order"


@pytest.mark.parametrize("scope", [None, "run_cumulative"])
def test_latest_result_wins_without_summing_cumulative_values(scope) -> None:
    run = make_run()
    telemetry = build_run_telemetry(
        run,
        [
            event(
                run,
                1,
                EventType.RUN_RESULT,
                5_000,
                {
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 2,
                    "total_cost_usd": 0.1,
                    "usage": {"input_tokens": 10},
                    "result_scope": scope,
                },
            ),
            event(
                run,
                2,
                EventType.RUN_RESULT,
                9_000,
                {
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 5,
                    "total_cost_usd": 0.2,
                    "usage": {"input_tokens": 30},
                    "result_scope": scope,
                },
            ),
        ],
    )

    assert telemetry.outcome.turn_count.value == 5
    assert telemetry.tokens.input_tokens.value == 30
    assert telemetry.cost.estimated_cost_usd.value == pytest.approx(0.2)
    assert ("multiple_run_results" in {
        warning.code for warning in telemetry.quality_warnings
    }) == (scope is None)
    assert telemetry.outcome.turn_count.scope == (scope or "sdk_query")


def test_foreign_run_events_are_rejected() -> None:
    run = make_run()
    other = make_run().model_copy(update={"id": "run_other"})

    with pytest.raises(ValueError, match="do not belong"):
        build_run_telemetry(
            run,
            [event(other, 1, EventType.RUN_STARTED, 1_000)],
        )
