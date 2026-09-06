"""Project-scoped control-plane aggregates, without invoking a model runtime."""

import json
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, signed_headers, trusted_settings
from test_telemetry import BASE, event, make_run

from ophira_backend.config import Settings
from ophira_backend.constants import (
    TOOL_CASHFLOW_PEAK,
    EventType,
    PrincipalRole,
    ProviderErrorCode,
    RunStatus,
)
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate
from ophira_backend.project_metrics import (
    MAX_DIMENSION_GROUPS,
    MetricsSource,
    build_project_metrics,
    read_project_metrics,
)
from ophira_backend.storage import EventReplayBudget, EventReplayLimitError, FileLedger


def aggregate(*sources):
    return build_project_metrics(
        "project_telemetry", list(sources), created_since=BASE - timedelta(days=7),
        created_until=BASE + timedelta(days=1), observed_at=BASE + timedelta(days=1),
    )


def result_event(run, sequence, cost, **extra):
    return event(run, sequence, EventType.RUN_RESULT, sequence * 1_000, {
        "subtype": "success", "is_error": False, "total_cost_usd": cost,
        "cost_basis": "sdk_anthropic_list_price", "result_scope": "run_cumulative",
        **extra,
    })


def test_latest_cumulative_cost_counts_each_run_once_even_with_the_same_sdk_session():
    first = make_run().model_copy(update={"sdk_session_id": "shared-session"})
    second = make_run().model_copy(update={"id": "run_second", "sdk_session_id": "shared-session"})
    output = aggregate(
        MetricsSource(first, [result_event(first, 1, 0.1), result_event(first, 2, 0.4)]),
        MetricsSource(second, [result_event(second, 1, 0.2)]),
    )
    assert output.summary.run_count == 2
    assert output.summary.cost_estimate_usd.observed_count == 2
    assert output.summary.cost_estimate_usd.observed_total == pytest.approx(0.6)
    assert output.providers[0].dimensions == {
        "provider_profile_id": "provider_a", "configured_model": "configured-model",
    }
    assert output.providers[0].runs.cost_estimate_usd.observed_total == pytest.approx(0.6)


def test_missing_cost_and_outcomes_are_not_zero_or_business_success():
    failed = make_run(status=RunStatus.FAILED).model_copy(update={
        "error_code": ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
        "error": "private failure body",
    })
    active = make_run(status=RunStatus.RUNNING, finished_at=None).model_copy(update={
        "id": "run_active", "provider_snapshot": None, "provider_profile_id": None,
    })
    cancelled = make_run(status=RunStatus.CANCELLED).model_copy(update={"id": "run_cancelled"})
    output = aggregate(MetricsSource(failed, []), MetricsSource(active, []),
                       MetricsSource(cancelled, []))
    assert output.summary.cost_estimate_usd.observed_total is None
    assert output.summary.cost_estimate_usd.unavailable_count == 3
    assert output.summary.execution_failure_rate.denominator == 1
    assert output.summary.execution_failure_rate.value == 1
    assert output.summary.status_counts["cancelled"] == 1
    assert output.summary.duration_ms.unavailable_count == 1
    assert output.models[0].dimensions == {"observed_model": None}
    assert output.errors[0].code == ProviderErrorCode.AUTH_REVOKED_OR_INVALID
    assert output.business_outcome_available is False
    assert "private failure body" not in output.model_dump_json()


def test_model_groups_use_observations_and_do_not_duplicate_whole_run_cost():
    run = make_run()
    output = aggregate(MetricsSource(run, [result_event(run, 1, 0.8, model_usage={
        "observed-a": {"costUSD": 0.3}, "observed-b": {"costUSD": 0.5},
    })]))
    assert {item.dimensions["observed_model"] for item in output.models} == {
        "observed-a", "observed-b",
    }
    assert all(item.runs.run_count == 1 for item in output.models)
    assert all(item.runs.cost_estimate_usd is None for item in output.models)
    assert output.summary.cost_estimate_usd.observed_total == 0.8


def test_skills_require_started_calls_and_conflicting_tool_lifecycles_stay_unknown():
    run = make_run().model_copy(update={"skill_snapshot": [
        {"id": "ophira-core:market-research"}, {"id": "ophira-core:supplier-due-diligence"},
    ]})
    rows = []

    def add(kind, tool_id, name, **extra):
        rows.append(event(run, len(rows) + 1, kind, (len(rows) + 1) * 100,
                          {"tool_name": name, **extra}, tool_use_id=tool_id))

    add(EventType.TOOL_PLANNED, "skill-unstarted", "Skill",
        tool_input={"skill": "ophira-core:supplier-due-diligence"})
    add(EventType.TOOL_STARTED, "skill-call", "Skill", tool_input={
        "skill": "ophira-core:market-research", "args": "private business prompt",
    })
    add(EventType.TOOL_COMPLETED, "skill-call", "Skill")
    add(EventType.TOOL_STARTED, "skill-other", "Skill",
        tool_input={"skill": "uninstalled-private-name"})
    add(EventType.TOOL_COMPLETED, "skill-other", "Skill")
    add(EventType.TOOL_STARTED, "mcp-conflict", TOOL_CASHFLOW_PEAK)
    add(EventType.TOOL_STARTED, "mcp-conflict", "mcp__ophira_commerce_math__calculate_landed_cost")
    add(EventType.TOOL_COMPLETED, "mcp-conflict", TOOL_CASHFLOW_PEAK)
    output = aggregate(MetricsSource(run, rows))
    skills = {item.identifier: item for item in output.skills}
    assert set(skills) == {None, "ophira-core:market-research"}
    assert skills["ophira-core:market-research"].invocations.completed_count == 1
    assert skills[None].invocations.unknown_count == 1
    assert output.mcp_tools[0].identifier is None
    assert output.mcp_tools[0].invocations.invocation_count == 1
    assert output.mcp_tools[0].invocations.unknown_count == 1
    assert output.mcp_tools[0].invocations.failure_rate.value is None
    serialized = output.model_dump_json()
    assert "private business prompt" not in serialized
    assert "uninstalled-private-name" not in serialized


def test_partial_foreign_or_duplicate_replays_do_not_create_observed_cost():
    run = make_run()
    valid = result_event(run, 1, 0.4)
    for rows in ([valid, valid], [valid.model_copy(update={"project_id": "other-project"})],
                 [valid.model_copy(update={"sequence": 2})]):
        output = aggregate(MetricsSource(run, list(rows)))
        assert output.summary.cost_estimate_usd.observed_total is None
        assert output.coverage.unavailable_event_replays == 1
    foreign = run.model_copy(update={"project_id": "other-project"})
    with pytest.raises(ValueError, match="project"):
        aggregate(MetricsSource(foreign, []))


def test_identifier_fields_do_not_echo_prose_urls_or_secrets():
    run = make_run(status=RunStatus.FAILED).model_copy(update={
        "prompt": "PRIVATE-PROMPT-CANARY", "error": "PRIVATE-ERROR-CANARY",
        "provider_snapshot": {"id": "provider_a", "model": "https://private.test/key?q=secret",
                              "display_name": "PRIVATE-DISPLAY-CANARY",
                              "base_url": "https://private.test/PRIVATE-URL-CANARY"},
    })
    output = aggregate(MetricsSource(run, [result_event(run, 1, None, model_usage={
        "user: PRIVATE-MODEL-CANARY": {"costUSD": None},
    }, result="PRIVATE-RESULT-CANARY", errors=["PRIVATE-ERRORS-CANARY"])]))
    serialized = output.model_dump_json()
    assert "PRIVATE-" not in serialized and "private.test" not in serialized
    assert output.errors[0].code is None
    assert output.models[0].dimensions["observed_model"] is None


def test_bounded_event_reads_keep_default_semantics_and_charge_one_request_budget(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="bounded", objective="bounded"))
    runs = [ledger.create_run(project.id, "private") for _ in range(2)]
    for run in runs:
        for index in range(2):
            ledger.append_event(event(run, index + 1, EventType.RUN_STARTED, index))
    budget = EventReplayBudget(max_bytes=20_000, max_events=3)
    assert len(ledger.list_events(runs[0].id, max_bytes=10_000, max_events=2,
                                  budget=budget)) == 2
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(runs[1].id, max_bytes=10_000, max_events=2, budget=budget)
    assert error.value.reason == "request_event_limit"
    assert budget.events_read == 3
    assert len(ledger.list_events(runs[1].id)) == 2
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(runs[0].id, max_bytes=1)
    assert error.value.reason == "run_byte_limit"


@pytest.mark.asyncio
async def test_project_metrics_api_enforces_membership_and_query_bounds(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    owner = principal("owner@example.com", PrincipalRole.OWNER)
    outsider = principal("outsider@example.com", PrincipalRole.OWNER)
    own = ledger.create_project(ProjectCreate(name="own", objective="private"), principal=owner)
    other = ledger.create_project(
        ProjectCreate(name="other", objective="private"), principal=outsider,
    )
    app.state.project_access.bind_project(own.id, owner)
    app.state.project_access.bind_project(other.id, outsider)
    path = f"/api/projects/{own.id}/metrics"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await request_as(client, "GET", path, owner.principal_id, owner.role)
        assert response.status_code == 200
        assert response.json()["summary"]["run_count"] == 0
        assert response.headers["cache-control"] == "no-store"
        denied = await request_as(client, "GET", path, outsider.principal_id, outsider.role)
        assert denied.status_code == 403
        for query in ("days=0", "days=32", "max_runs=201", "until=2026-09-02T12:00:00"):
            bad = await request_as(client, "GET", f"{path}?{query}", owner.principal_id, owner.role)
            assert bad.status_code == 422


def test_selection_is_utc_creation_window_and_reports_truncated_run_coverage(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="window", objective="window"))
    for offset in (-8, -3, -2, -1, 0, 1):
        run = ledger.create_run(project.id, "window private")
        ledger.update_run(run.id, created_at=BASE + timedelta(days=offset))
    output = read_project_metrics(
        ledger, project.id, created_since=BASE - timedelta(days=7), created_until=BASE,
        observed_at=BASE, max_runs=2,
    )
    assert output.summary.run_count == 2
    assert output.coverage.matching_runs == 3
    assert output.coverage.omitted_by_run_limit == 1
    assert output.window.created_since.isoformat() == (BASE - timedelta(days=7)).isoformat()
    assert "window private" not in json.dumps(output.model_dump(mode="json"))


def test_percentiles_use_only_observed_samples_and_report_the_missing_denominator():
    sources = []
    for seconds in range(1, 21):
        run = make_run(started_at=BASE, finished_at=BASE + timedelta(seconds=seconds)).model_copy(
            update={"id": f"run_duration_{seconds}"},
        )
        sources.append(MetricsSource(run, []))
    sources.append(MetricsSource(make_run(started_at=None, finished_at=None), []))
    output = aggregate(*sources)
    assert output.summary.duration_ms.observed_count == 20
    assert output.summary.duration_ms.unavailable_count == 1
    assert output.summary.duration_ms.p50 == 10_000
    assert output.summary.duration_ms.p95 == 19_000
    assert output.summary.duration_ms.observed_total == 210_000


def test_crash_zero_cost_stays_unknown_and_typed_mcp_error_is_execution_failure():
    run = make_run(status=RunStatus.FAILED)
    rows = [
        event(run, 1, EventType.TOOL_STARTED, 1_000,
              {"tool_name": TOOL_CASHFLOW_PEAK}, tool_use_id="math"),
        event(run, 2, EventType.TOOL_COMPLETED, 1_100, {
            "tool_name": TOOL_CASHFLOW_PEAK, "tool_response": {"isError": True},
        }, tool_use_id="math"),
        result_event(run, 3, 0, subtype="error_during_execution", is_error=True),
        event(run, 4, EventType.RUN_FAILED, 4_000, {
            "error_code": ProviderErrorCode.SDK_RUNTIME_FAILED,
            "error": "PRIVATE-TOOL-ERROR-CANARY",
        }),
    ]
    output = aggregate(MetricsSource(run, rows))
    assert output.summary.cost_estimate_usd.observed_total is None
    assert output.mcp_tools[0].identifier == TOOL_CASHFLOW_PEAK
    assert output.mcp_tools[0].invocations.failed_count == 1
    assert output.mcp_tools[0].invocations.failure_rate.value == 1
    assert output.mcp_tools[0].invocations.duration_ms.p95 == 100
    assert output.errors[0].code == ProviderErrorCode.SDK_RUNTIME_FAILED
    assert "PRIVATE-TOOL-ERROR-CANARY" not in output.model_dump_json()


@pytest.mark.parametrize("unknown_first", [False, True])
def test_group_overflow_collapses_to_unknown_without_unbounded_output(unknown_first):
    run = make_run()
    rows = [event(run, index + 1, EventType.ASSISTANT_MESSAGE_COMPLETED, index,
                  {"model": f"observed-{index:03}", "text": "private text"})
            for index in range(MAX_DIMENSION_GROUPS + 9)]
    if unknown_first:
        rows.append(event(run, len(rows) + 1, EventType.ASSISTANT_MESSAGE_COMPLETED, len(rows),
                          {"model": "not a safe model identifier"}))
    output = aggregate(MetricsSource(run, rows))
    assert len(output.models) == MAX_DIMENSION_GROUPS + 1
    assert output.coverage.group_overflow_associations == 9
    assert next(item for item in output.models if item.dimensions["observed_model"] is None
                ).runs.run_count == 1


def test_request_budget_discards_partial_run_replay_and_preserves_unknown_cost(
    tmp_path, monkeypatch,
):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="budget", objective="budget"))
    for index in range(3):
        run = ledger.create_run(project.id, "private")
        ledger.update_run(run.id, created_at=BASE - timedelta(hours=index + 1))
        ledger.append_event(event(run, 1, EventType.RUN_STARTED, 1))
        ledger.append_event(result_event(run, 2, 0.3))
    monkeypatch.setattr("ophira_backend.project_metrics.MAX_REQUEST_EVENTS", 3)
    output = read_project_metrics(
        ledger, project.id, created_since=BASE - timedelta(days=1), created_until=BASE,
        observed_at=BASE,
    )
    assert output.coverage.event_count_read == 3
    assert output.coverage.unavailable_reasons == {"request_event_limit": 2}
    assert output.summary.cost_estimate_usd.observed_count == 1
    assert output.summary.cost_estimate_usd.unavailable_count == 2
    assert output.summary.cost_estimate_usd.observed_total == 0.3


def test_invalid_timezone_is_reported_without_breaking_valid_run_selection(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="dates", objective="dates"))
    for value in (BASE - timedelta(hours=1), BASE.replace(tzinfo=None)):
        run = ledger.create_run(project.id, "private")
        ledger.update_run(run.id, created_at=value)
    output = read_project_metrics(
        ledger, project.id, created_since=BASE - timedelta(days=1), created_until=BASE,
        observed_at=BASE,
    )
    assert output.coverage.invalid_created_at_runs == 1
    assert output.coverage.matching_runs == output.summary.run_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_after", [1, 2])
async def test_metrics_read_rechecks_revoked_membership_and_rejects_other_tenant(
    tmp_path, monkeypatch, revoke_after,
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    owner = principal("owner@example.com", PrincipalRole.OWNER)
    viewer = principal("viewer@example.com", PrincipalRole.VIEWER)
    project = ledger.create_project(ProjectCreate(name="private", objective="private"),
                                    principal=owner)
    access = app.state.project_access
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id=viewer.principal_id, role=viewer.role)
    path = f"/api/projects/{project.id}/metrics"
    original = access.authorize
    calls = []

    def revoke_after_first_authorization(*args, **kwargs):
        allowed = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == revoke_after:
            access.revoke(project.id, owner, principal_id=viewer.principal_id)
        return allowed

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        foreign = await client.get(path, headers=signed_headers(
            "GET", path, viewer.principal_id, viewer.role, tenant_id="other_tenant",
        ))
        assert foreign.status_code in {401, 403}
        monkeypatch.setattr(access, "authorize", revoke_after_first_authorization)
        response = await request_as(client, "GET", path, viewer.principal_id, viewer.role)
        assert response.status_code == 403
        assert response.json() == {"detail": "principal is not authorized for this resource"}


def test_project_metrics_never_merges_a_foreign_tenant_run(tmp_path, monkeypatch):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="private", objective="private"))
    run = ledger.create_run(project.id, "private").model_copy(update={"tenant_id": "foreign"})
    monkeypatch.setattr(ledger, "list_runs", lambda *_args, **_kwargs: [run])
    with pytest.raises(ValueError, match="identity"):
        read_project_metrics(ledger, project.id, created_since=BASE - timedelta(days=1),
                             created_until=BASE, observed_at=BASE)


@pytest.mark.parametrize(("flags", "outcome"), [
    ({"isError": True}, "failed"), ({"is_error": True}, "failed"),
    ({"isError": False}, "completed"), ({"is_error": False}, "completed"),
    ({"isError": True, "is_error": True}, "failed"),
    ({"isError": False, "is_error": False}, "completed"),
    ({"isError": False, "is_error": True}, "unknown"),
    ({"isError": "true"}, "unknown"), ({"is_error": 1}, "unknown"),
    ({"isError": True, "is_error": None}, "unknown"),
])
def test_mcp_error_envelope_flags_are_strict_and_conflicts_remain_unknown(flags, outcome):
    run = make_run()
    rows = [
        event(run, 1, EventType.TOOL_STARTED, 1,
              {"tool_name": TOOL_CASHFLOW_PEAK}, tool_use_id="math"),
        event(run, 2, EventType.TOOL_COMPLETED, 2, {
            "tool_name": TOOL_CASHFLOW_PEAK,
            "tool_response": {**flags, "content": [{"type": "text", "text": "private"}]},
        }, tool_use_id="math"),
    ]
    invocation = aggregate(MetricsSource(run, rows)).mcp_tools[0].invocations
    assert getattr(invocation, f"{outcome}_count") == 1
    assert invocation.failure_rate.denominator == (0 if outcome == "unknown" else 1)


@pytest.mark.asyncio
async def test_populated_metrics_route_contains_only_the_selected_project(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    selected = ledger.create_project(ProjectCreate(name="selected", objective="private selected"))
    foreign = ledger.create_project(ProjectCreate(name="foreign", objective="private foreign"))
    for project, cost in ((selected, 0.25), (foreign, 900)):
        run = ledger.create_run(project.id, f"PRIVATE-PROMPT-{project.id}",
                                provider_profile_id="frozen-provider", provider_snapshot={
                                    "id": "frozen-provider", "model": "frozen-model",
                                    "display_name": "PRIVATE-PROVIDER-DISPLAY",
                                })
        ledger.append_event(result_event(run, 1, cost, result="PRIVATE-RESULT"))
        ledger.update_run(run.id, status=RunStatus.COMPLETED)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/api/projects/{selected.id}/metrics?days=7")
        assert response.status_code == 200
        result = response.json()
        assert result["summary"]["run_count"] == 1
        assert result["summary"]["cost_estimate_usd"]["observed_total"] == 0.25
        assert result["summary"]["duration_ms"]["observed_count"] == 0
        assert result["coverage"]["matching_runs"] == result["coverage"]["selected_runs"] == 1
        assert result["coverage"]["event_count_read"] == 1
        assert result["providers"][0]["dimensions"] == {
            "provider_profile_id": "frozen-provider", "configured_model": "frozen-model",
        }
        assert foreign.id not in response.text and "PRIVATE-" not in response.text
        assert (await client.get("/api/projects/missing_project/metrics")).status_code == 404
