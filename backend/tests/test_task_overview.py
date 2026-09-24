import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend.constants import EventType, PrincipalRole
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import PendingPermissionView, ProjectCreate
from ophira_backend.project_lifecycle import ProjectUnavailableError
from ophira_backend.storage import FileLedger
from ophira_backend.task_overview import read_task_overview


@pytest.mark.asyncio
async def test_task_list_is_run_scoped_with_cost_result_and_approvals(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Market", objective="Research"))
    other = ledger.create_project(ProjectCreate(name="Private", objective="Research"))
    run = ledger.create_run(project.id, "Compare ceramics", max_budget_usd=2)
    ledger.create_run(other.id, "Secret")
    await EventPublisher(ledger).publish(run.id, EventType.RUN_RESULT, {
        "result": "Ceramics look promising", "total_cost_usd": 0.15,
    })
    pending = PendingPermissionView(
        id="permission_1", run_id=run.id, project_id=project.id, tool_name="Write", scope=".",
        destructive=False, persistable=False,
    )
    overview = read_task_overview(
        ledger, can_read_run=lambda project_id: project_id == project.id,
        pending=[pending],
    )
    assert overview.total_visible_runs == 1 and not overview.truncated
    row = overview.rows[0]
    assert row.run_id == run.id and row.pending_approvals == 1
    assert row.estimated_cost_usd == 0.15 and row.max_budget_usd == 2
    assert row.result_preview == "Ceramics look promising"
    assert read_task_overview(
        FileLedger(tmp_path), can_read_run=lambda _: True, pending=[],
    ).total_visible_runs == 2


@pytest.mark.asyncio
async def test_tasks_api_filters_project_membership_before_reading_results(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        created = await request_as(
            client, "POST", "/api/projects", "owner@example.com", PrincipalRole.OWNER,
            json={"name": "Market", "objective": "Research"},
        )
        project_id = created.json()["id"]
        run = app.state.ledger.create_run(
            project_id, "Private ceramic plan",
            principal=principal("owner@example.com", PrincipalRole.OWNER),
        )
        await EventPublisher(app.state.ledger).publish(
            run.id, EventType.RUN_RESULT, {"result": "Private result"},
        )
        owner = await request_as(
            client, "GET", "/api/tasks", "owner@example.com", PrincipalRole.OWNER,
        )
        assert owner.status_code == 200 and owner.json()["rows"][0]["run_id"] == run.id
        assert owner.headers["cache-control"] == "no-store"
        outsider = await request_as(
            client, "GET", "/api/tasks", "outsider@example.com", PrincipalRole.MEMBER,
        )
        assert outsider.status_code == 200 and outsider.json()["rows"] == []
        assert "Private" not in outsider.text


def test_task_preview_redacts_before_truncating(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Market", objective="Research"))
    ledger.create_run(project.id, "A" * 295 + " Bearer example-secret-token")
    overview = read_task_overview(
        ledger, can_read_run=lambda _: True, pending=[],
    )
    assert "example-secret-token" not in overview.rows[0].prompt


def test_task_list_skips_a_project_deleted_between_access_check_and_read(tmp_path, monkeypatch):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Market", objective="Research"))
    ledger.create_run(project.id, "Review")

    def unavailable_project(_project_id: str):
        raise ProjectUnavailableError(project.id)

    monkeypatch.setattr(ledger, "get_project", unavailable_project)
    overview = read_task_overview(ledger, can_read_run=lambda _: True, pending=[])
    assert overview.rows == [] and overview.truncated


@pytest.mark.asyncio
async def test_task_list_reports_row_and_event_replay_limits(tmp_path, monkeypatch):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Market", objective="Research"))
    runs = [ledger.create_run(project.id, f"Task {index}") for index in range(51)]
    await EventPublisher(ledger).publish(
        runs[-1].id, EventType.RUN_RESULT, {"result": "A result beyond the event limit"},
    )
    monkeypatch.setattr("ophira_backend.task_overview.MAX_RUN_EVENT_BYTES", 20)

    overview = read_task_overview(ledger, can_read_run=lambda _: True, pending=[])

    assert overview.total_visible_runs == 51
    assert len(overview.rows) == 50
    assert overview.truncated
    newest = next(row for row in overview.rows if row.run_id == runs[-1].id)
    assert not newest.observations_available
    assert newest.result_preview is None
