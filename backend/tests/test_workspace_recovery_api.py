# ruff: noqa: F811 -- imported pytest fixture
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_artifact_api import source  # noqa: F401
from test_auth_api import principal, request_as

from ophira_backend.artifact_files import freeze_artifact, snapshot_relative
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.models import utc_now
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


async def test_retention_requires_administrator_and_exact_confirmation(ready):
    app, owner, project, point, artifact = ready
    service = app.state.workspace_checkpoints
    member = principal("member", PrincipalRole.MEMBER)
    app.state.project_access.grant(
        project.id, owner, principal_id=member.principal_id, role=member.role,
    )
    base = f"/api/projects/{project.id}/workspace"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        denied = await call(client, "GET", base + "/retention", member)
        assert denied.status_code == 403
        inventory = await call(client, "GET", base + "/retention", owner)
        assert inventory.status_code == 200
        assert inventory.headers["cache-control"] == "no-store"
        assert inventory.json()["checkpoints"][0]["id"] == point
        target = base + f"/retention/checkpoints/{point}/delete"
        fingerprint = inventory.json()["checkpoints"][0]["fingerprint"]
        for confirmation in (False, 1, None):
            invalid = await call(client, "POST", target, owner, json={
                "expected_fingerprint": fingerprint, "confirm_delete": confirmation,
            })
            assert invalid.status_code == 422
        denied = await call(client, "POST", target, member, json={
            "expected_fingerprint": fingerprint, "confirm_delete": True,
        })
        assert denied.status_code == 403
        reviewed = await call(client, "POST", base + "/restores", owner,
                              json={"checkpoint_id": point})
        assert reviewed.status_code == 200
        protected = await call(client, "POST", target, owner, json={
            "expected_fingerprint": fingerprint, "confirm_delete": True,
        })
        assert protected.status_code == 409
        preview = service.get_restore(project.id, reviewed.json()["id"])
        preview.expires_at = utc_now() - timedelta(seconds=1)
        service._save_restore(preview)
        inventory = await call(client, "GET", base + "/retention", owner)
        row = inventory.json()["reviews"][0]
        deleted = await call(client, "POST", base + f"/retention/restores/{row['id']}/delete",
                             owner, json={"expected_fingerprint": row["fingerprint"],
                                          "confirm_delete": True})
        assert deleted.status_code == 200 and deleted.json()["reviews"] == []
        deleted = await call(client, "POST", target, owner, json={
            "expected_fingerprint": fingerprint, "confirm_delete": True,
        })
        assert deleted.status_code == 200 and deleted.json()["checkpoints"] == []
        working = app.state.ledger.workspace_for_project(project.id) / artifact.path
        assert working.read_bytes() == (
            b"Changed"
        )


async def test_retention_can_discard_duplicate_copy_without_losing_safety_point(ready):
    app, owner, project, point, _ = ready
    base = f"/api/projects/{project.id}/workspace"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        preview = await call(client, "POST", base + "/restores", owner,
                             json={"checkpoint_id": point})
        applied = await call(client, "POST", base + f"/restores/{preview.json()['id']}/apply",
                             owner, json={"confirm_file_changes": True})
        assert applied.status_code == 200
        inventory = await call(client, "GET", base + "/retention", owner)
        row = inventory.json()["reviews"][0]
        assert inventory.json()["retained_bytes"] > 0
        discarded = await call(
            client, "POST", base + f"/retention/restores/{row['id']}/discard-copy", owner,
            json={"expected_fingerprint": row["fingerprint"], "confirm_delete": True},
        )
        assert discarded.status_code == 200
        assert discarded.json()["retained_bytes"] == 0
        assert discarded.json()["reviews"][0]["retained_discarded_at"]
        assert app.state.workspace_checkpoints.load(
            project.id, applied.json()["safety_checkpoint_id"],
        )
        status = await call(client, "GET", base + f"/restores/{row['id']}", owner)
        assert status.status_code == 200 and status.json()["state"] == "applied"
        row = discarded.json()["reviews"][0]
        archived = await call(
            client, "POST", base + f"/retention/restores/{row['id']}/archive", owner,
            json={"expected_fingerprint": row["fingerprint"], "confirm_delete": True},
        )
        assert archived.status_code == 200
        assert archived.json()["reviews"] == [] and archived.json()["archived_reviews"] == 1
        status = await call(client, "GET", base + f"/restores/{row['id']}", owner)
        assert status.status_code == 200 and status.json()["archived_at"] is not None


async def test_retention_reclaims_only_unreferenced_snapshot_files(ready):
    app, owner, project, point, _ = ready
    root = app.state.ledger._project_path(project.id).parent
    orphan = freeze_artifact(root, b"unreferenced test content")
    base = f"/api/projects/{project.id}/workspace/retention"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        inventory = await call(client, "GET", base, owner)
        assert inventory.status_code == 200
        assert inventory.json()["reclaimable_files"] == 1
        invalid = await call(client, "POST", base + "/snapshots/reclaim", owner, json={
            "expected_fingerprint": inventory.json()["reclaim_fingerprint"],
            "confirm_delete": False,
        })
        assert invalid.status_code == 422
        reclaimed = await call(client, "POST", base + "/snapshots/reclaim", owner, json={
            "expected_fingerprint": inventory.json()["reclaim_fingerprint"],
            "confirm_delete": True,
        })
        assert reclaimed.status_code == 200
        assert reclaimed.json()["reclaimable_files"] == 0
        assert not (root / snapshot_relative(orphan)).exists()
        assert app.state.workspace_checkpoints.load(project.id, point)
