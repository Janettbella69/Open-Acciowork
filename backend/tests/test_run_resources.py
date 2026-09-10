from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_telemetry import BASE, event, make_run

from ophira_backend import run_resources
from ophira_backend.constants import EventType, PrincipalRole, RunMode, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate
from ophira_backend.run_resources import build_run_resources, read_run_resources


def research_run():
    return make_run(status=RunStatus.RUNNING, finished_at=None).model_copy(update={
        "mode": RunMode.DEEP_RESEARCH, "sdk_budget_usd": 2.0,
        "sdk_wall_clock_deadline": BASE + timedelta(minutes=30),
    })


def observations(run):
    return [
        event(run, 1, EventType.TOOL_STARTED, 2000, {
            "tool_name": "Agent", "tool_input": {"subagent_type": "source-mapper"},
        }, tool_use_id="agent_dispatch_1"),
        event(run, 2, EventType.SUBAGENT_STARTED, 2100, {
            "agent_id": "native_agent", "agent_type": "source-mapper",
        }),
        event(run, 3, EventType.TASK_STARTED, 2200, {
            "task_id": "task_1", "task_type": "local_agent", "description": "Map original sources",
        }, tool_use_id="agent_dispatch_1"),
        event(run, 4, EventType.TASK_PROGRESS, 3000, {
            "task_id": "task_1", "description": "Reading sources", "last_tool_name": "Read",
            "usage": {"total_tokens": 100, "tool_uses": 1, "duration_ms": 800},
        }, tool_use_id="agent_dispatch_1"),
        event(run, 5, EventType.TASK_COMPLETED, 4000, {
            "task_id": "task_1", "status": "completed",
            "usage": {"total_tokens": 180, "tool_uses": 2, "duration_ms": 1800},
            "summary": "This summary is not copied to the resource API",
        }, tool_use_id="agent_dispatch_1"),
        event(run, 6, EventType.SUBAGENT_STOPPED, 4100, {
            "agent_id": "native_agent", "agent_type": "source-mapper",
        }),
        event(run, 7, EventType.RUN_RESULT, 5000, {
            "result_scope": "run_cumulative", "total_cost_usd": 0.5,
            "cost_basis": "sdk_anthropic_list_price", "subtype": "success",
            "usage": {"input_tokens": 300, "output_tokens": 20},
            "model_usage": {"model": {"inputTokens": 500, "outputTokens": 80}},
        }),
    ]


def test_native_progress_snapshots_are_not_summed_or_allocated_run_cost():
    run = research_run()
    value = build_run_resources(run, observations(run), observed_at=BASE + timedelta(seconds=15))
    assert value.observations_available
    assert value.limits.frozen and value.limits.turn_limit is None
    assert value.remaining_budget_usd == 1.5
    assert value.elapsed_seconds == 14 and value.remaining_seconds == 1785
    assert value.tasks[0].usage.total_tokens == 180  # not 100 + 180
    assert value.tasks[0].requested_agent_type == "source-mapper"
    assert value.tasks[0].status == "completed" and value.agents[0].state == "stopped"
    assert value.tokens.input_tokens.value == 500
    assert len(value.research_units) == 3 and len(value.agents) == 1
    assert "summary is not copied" not in value.model_dump_json()
    assert "cost" not in value.tasks[0].model_dump()


def test_later_result_replaces_previous_cumulative_cost_and_resume_retains_original_limits():
    run = research_run()
    rows = observations(run)
    rows.append(event(run, 8, EventType.RUN_RESULT, 6000, {
        "result_scope": "run_cumulative", "total_cost_usd": 0.8, "subtype": "success",
    }))
    value = build_run_resources(run, rows, observed_at=BASE + timedelta(minutes=29))
    assert value.remaining_budget_usd == 1.2 and value.remaining_seconds == 60
    assert value.cost.estimated_cost_usd.value == 0.8


@pytest.mark.parametrize("raw", [
    {"total_tokens": 50}, {"tool_uses": True}, {"duration_ms": -1},
    {"total_tokens": float("inf")}, {"total_tokens": {"nested": "untrusted"}},
])
def test_malformed_or_regressing_usage_never_becomes_a_live_total(raw):
    run = research_run()
    rows = observations(run)
    rows[4].data["usage"] = raw
    value = build_run_resources(run, rows, observed_at=BASE)
    assert value.tasks[0].usage.availability == "inconsistent"
    assert value.tasks[0].usage.total_tokens is None
    assert value.tasks[0].warnings


def test_missing_final_task_usage_keeps_the_labeled_prior_snapshot():
    run = research_run()
    rows = observations(run)
    rows[4].data["usage"] = None
    value = build_run_resources(run, rows, observed_at=BASE)
    assert value.tasks[0].usage.total_tokens == 100
    assert value.tasks[0].usage.source_sequence == 4
    assert value.tasks[0].status == "completed"


def test_missing_result_and_terminal_task_observation_are_not_filled_with_success_or_zero():
    run = research_run().model_copy(update={"status": RunStatus.COMPLETED,
                                            "finished_at": BASE + timedelta(seconds=6)})
    value = build_run_resources(run, observations(run)[:4], observed_at=BASE + timedelta(days=1))
    assert value.remaining_budget_usd is None
    assert value.remaining_seconds is None and value.elapsed_seconds == 5
    assert value.tasks[0].status == "running"  # last observed state, not inferred success
    assert value.cost.estimated_cost_usd.availability == "unavailable"


@pytest.mark.parametrize("mutation", ["gap", "duplicate_id", "duplicate_sequence", "huge_sequence"])
def test_incomplete_log_hides_aggregates_but_preserves_frozen_limits(mutation):
    run = research_run()
    rows = observations(run)
    if mutation == "gap":
        rows.pop(2)
    elif mutation == "duplicate_id":
        rows[3].id = rows[2].id
    else:
        rows[3].sequence = rows[2].sequence if mutation == "duplicate_sequence" else 10**15
    value = build_run_resources(run, rows, observed_at=BASE)
    assert not value.observations_available
    assert value.tasks == [] and value.cost.estimated_cost_usd.value is None
    assert value.limits.sdk_budget_usd == 2


@pytest.mark.parametrize("identity_field", ["trace_id", "project_id", "run_id"])
def test_foreign_events_rejected_and_unmatched_tasks_do_not_inherit_types(identity_field):
    run = research_run()
    rows = observations(run)
    rows[2].tool_use_id = rows[3].tool_use_id = rows[4].tool_use_id = "unmatched"
    value = build_run_resources(run, rows, observed_at=BASE)
    assert value.tasks[0].requested_agent_type is None
    setattr(rows[0], identity_field, "another_identity")
    with pytest.raises(ValueError, match="outside_run"):
        build_run_resources(run, rows, observed_at=BASE)


def test_task_and_agent_counts_are_bounded(monkeypatch):
    run = research_run()
    monkeypatch.setattr(run_resources, "MAX_RESOURCE_TASKS", 0)
    for rows in [observations(run)[:2], [event(run, 1, EventType.TASK_STARTED, 1,
                                            {"task_id": "task", "task_type": "local_bash"})]]:
        value = build_run_resources(run, rows, observed_at=BASE)
        assert not value.observations_available
        assert value.unavailable_reason == "native_task_count_limit"


@pytest.fixture
def source(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Research", objective="Resource visibility"), principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    run = app.state.ledger.create_run(
        project.id, "Observe", mode=RunMode.DEEP_RESEARCH, principal=owner,
    )
    app.state.ledger.update_run(run.id, sdk_budget_usd=2)
    return app, owner, project, run


async def test_resources_api_requires_scope_and_does_not_cache_private_data(source):
    app, owner, _, run = source
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client, "GET", f"/api/runs/{run.id}/resources", "owner", owner.role,
        )
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["limits"]["sdk_budget_usd"] == 2
        assert response.json()["remaining_budget_usd"] is None
        assert "principal_session_id" not in response.json()
        outsider = await request_as(
            client, "GET", f"/api/runs/{run.id}/resources", "other", owner.role,
        )
        assert outsider.status_code == 403
        unsigned = await client.get(f"/api/runs/{run.id}/resources")
        assert unsigned.status_code == 401


def test_replay_limits_preserve_limits_and_authorization_is_rechecked_after_read(
    source, monkeypatch,
):
    app, _, _, run = source
    ledger = app.state.ledger
    ledger.append_event(event(run, 1, EventType.TASK_STARTED, 1, {"task_id": "task"}))
    monkeypatch.setattr(run_resources, "MAX_RESOURCE_EVENT_BYTES", 1)
    value = read_run_resources(ledger, run.id, authorize=lambda _: None)
    assert not value.observations_available and value.unavailable_reason == "event_replay_limit"
    assert value.limits.sdk_budget_usd == 2
    calls = 0

    def revoked_after_read(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        read_run_resources(ledger, run.id, authorize=revoked_after_read)


async def test_resources_consume_actual_sdk_message_mapping_and_hook_contract(tmp_path):
    from claude_agent_sdk import (
        AssistantMessage,
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
        ToolUseBlock,
    )
    from test_sdk_adapter import build_runtime

    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH)
    await runtime._map_message(run.id, AssistantMessage(
        content=[ToolUseBlock(id="dispatch", name="Agent",
                              input={"subagent_type": "conflict-verifier"})], model="fixture",
    ))
    workspace = ledger.workspace_for_project(run.project_id)
    hook = runtime._hook(run.id, workspace)
    await hook({"hook_event_name": "SubagentStart", "agent_id": "agent_1",
                "agent_type": "conflict-verifier"}, None, {})
    for message in [
        TaskStartedMessage(subtype="task_started", data={}, task_id="task_1",
                           description="Verify", uuid="started", session_id="session",
                           tool_use_id="dispatch", task_type="local_agent"),
        TaskProgressMessage(subtype="task_progress", data={}, task_id="task_1",
                            description="Reading", uuid="progress", session_id="session",
                            tool_use_id="dispatch", usage={"total_tokens": 12,
                                                          "tool_uses": 1, "duration_ms": 50}),
        TaskNotificationMessage(subtype="task_notification", data={}, task_id="task_1",
                                status="completed", output_file="private-path", summary="Done",
                                uuid="done", session_id="session", tool_use_id="dispatch",
                                usage={"total_tokens": 20, "tool_uses": 2, "duration_ms": 75}),
    ]:
        await runtime._map_message(run.id, message)
    value = read_run_resources(ledger, run.id, authorize=lambda _: None)
    assert value.tasks[0].requested_agent_type == "conflict-verifier"
    assert value.tasks[0].usage.total_tokens == 20
    assert value.agents[0].agent_type == "conflict-verifier"
    assert "private-path" not in value.model_dump_json()
