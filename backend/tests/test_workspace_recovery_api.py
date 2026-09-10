# ruff: noqa: F811 -- imported pytest fixture
import pytest
from httpx import ASGITransport, AsyncClient
from test_artifact_api import source  # noqa: F401
from test_auth_api import principal, request_as

from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.workspace_checkpoints import workspace_lease


@pytest.fixture
def ready(source):
    app, owner, project, artifact = source
    ledger = app.state.ledger
    service = app.state.workspace_checkpoints
    run = ledger.create_run(project.id, "Saved starting state", principal=owner)
    service.prepare_run(run.id)
    ledger.update_run(run.id, status=RunStatus.FAILED)
    (ledger.workspace_for_project(project.id) / artifact.path).write_bytes(b"Changed")
    return app, owner, project, f"before-{run.id}", artifact


async def call(client, method, path, who, **kwargs):
    return await request_as(client, method, path, who.principal_id, who.role, **kwargs)


async def test_review_confirmation_recovery_and_retry_are_scoped_and_private(ready):
    app, owner, project, point, artifact = ready
    base = f"/api/projects/{project.id}/workspace"
    working = app.state.ledger.workspace_for_project(project.id) / artifact.path
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        points = await call(client, "GET", base + "/checkpoints", owner)
        assert points.status_code == 200 and points.json()[0]["id"] == point
        assert points.headers["cache-control"] == "no-store"
        reviewed = await call(client, "POST", base + "/restores", owner,
                              json={"checkpoint_id": point})
        assert reviewed.status_code == 200
        data = reviewed.json()
        assert data["state"] == "preview" and data["changes"][0]["action"] == "replace"
        assert "principal_session_id" not in data and "before_inode" not in data
        target = base + f"/restores/{data['id']}"
        for value in ({}, {"confirm_file_changes": False}, {"confirm_file_changes": 1}):
            invalid = await call(client, "POST", target + "/apply", owner, json=value)
            assert invalid.status_code == 422
        assert working.read_bytes() == b"Changed"
        applied = await call(client, "POST", target + "/apply", owner,
                             json={"confirm_file_changes": True})
        assert applied.status_code == 200 and applied.json()["state"] == "applied"
        again = await call(client, "POST", target + "/apply", owner,
                           json={"confirm_file_changes": True})
        assert again.json() == applied.json()
        status = await call(client, "GET", target, owner)
        assert status.json() == applied.json()
        assert working.read_bytes() == b"Initial report"


async def test_viewer_and_outsider_are_denied_and_active_runs_reject_recovery(ready):
    app, owner, project, point, _ = ready
    viewer = principal("viewer", PrincipalRole.VIEWER)
    app.state.project_access.grant(
        project.id, owner, principal_id=viewer.principal_id, role=viewer.role,
    )
    base = f"/api/projects/{project.id}/workspace"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        visible = await call(client, "GET", base + "/checkpoints", viewer)
        assert visible.status_code == 200
        denied = await call(client, "POST", base + "/restores", viewer,
                            json={"checkpoint_id": point})
        assert denied.status_code == 403
        outsider = await call(client, "GET", base + "/checkpoints",
                              principal("outsider", PrincipalRole.OWNER))
        assert outsider.status_code == 403
        unsigned = await client.get(base + "/checkpoints")
        assert unsigned.status_code == 401
        with workspace_lease(app.state.ledger, project.id):
            busy = await call(client, "POST", base + "/restores", owner,
                              json={"checkpoint_id": point})
        assert busy.status_code == 409
        app.state.ledger.create_run(project.id, "Queued work", principal=owner)
        busy = await call(client, "POST", base + "/restores", owner,
                          json={"checkpoint_id": point})
        assert busy.status_code == 409


async def test_revoked_member_cannot_apply_a_previous_review(ready):
    app, owner, project, point, artifact = ready
    member = principal("member", PrincipalRole.MEMBER)
    app.state.project_access.grant(
        project.id, owner, principal_id=member.principal_id, role=member.role,
    )
    base = f"/api/projects/{project.id}/workspace"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        reviewed = await call(client, "POST", base + "/restores", member,
                              json={"checkpoint_id": point})
        assert reviewed.status_code == 200
        app.state.project_access.revoke(project.id, owner, principal_id=member.principal_id)
        applied = await call(client, "POST", base + f"/restores/{reviewed.json()['id']}/apply",
                             member, json={"confirm_file_changes": True})
        assert applied.status_code == 403
    assert (app.state.ledger.workspace_for_project(project.id) / artifact.path).read_bytes() == (
        b"Changed"
    )
