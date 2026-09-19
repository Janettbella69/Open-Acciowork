"""Real route/auth/store contract, including native SDK erasure in isolated data."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, signed_headers, trusted_settings
from test_project_session_cleanup import native_session, write

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate


@pytest.mark.asyncio
async def test_project_delete_full_http_contract_and_real_native_cleanup(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Deletion HTTP fixture", objective="erase"))
    run, config, folder = native_session(tmp_path, project.id)
    stored = ledger.create_run(project.id, "fixture prompt")
    ledger.update_run(stored.id, sdk_session_id=run.sdk_session_id)
    write(config / "file-history" / run.sdk_session_id / "checkpoint", b"private checkpoint")
    root = f"/api/projects/{project.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        preview = await client.get(root + "/deletion")
        assert preview.status_code == 200, preview.text
        assert preview.headers["cache-control"] == "no-store"
        assert preview.json()["can_delete"]
        assert preview.json()["counts"]["sessions"] == 1
        payload = {
            "operation_id": str(uuid4()), "fingerprint": preview.json()["fingerprint"],
            "confirmation_name": project.name, "reason": "remove isolated test project",
        }
        deleted = await client.post(root + "/deletion", json=payload)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted", deleted.text
        assert not folder.exists()
        assert not (config / "file-history" / run.sdk_session_id).exists()
        assert (await client.get(root)).status_code == 404
        assert (await client.get(f"/api/runs/{stored.id}")).status_code == 404
        assert (await client.get(root + "/deletion")).status_code == 404
        operation = f"/api/project-deletions/{project.id}/{payload['operation_id']}"
        for method, path in (("GET", operation), ("POST", operation + "/retry")):
            response = await client.request(method, path)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "deleted"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.post(root + "/deletion", json=payload)).json() == deleted.json()


@pytest.mark.asyncio
async def test_delete_requires_owner_admin_and_receipt_is_bound_to_requester_session(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("alice", PrincipalRole.OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        created = await request_as(client, "POST", "/api/projects", "alice", PrincipalRole.OWNER,
                                   json={"name": "Shared private project", "objective": "erase"})
        assert created.status_code == 201, created.text
        project = created.json()
        app.state.project_access.grant(project["id"], owner, principal_id="bob",
                                       role=PrincipalRole.MEMBER)
        app.state.project_access.grant(project["id"], owner, principal_id="admin",
                                       role=PrincipalRole.ADMIN)
        root = f"/api/projects/{project['id']}/deletion"
        member = await request_as(client, "GET", root, "bob", PrincipalRole.MEMBER)
        assert member.status_code == 403
        assert member.headers["cache-control"] == "no-store"
        review = await request_as(client, "GET", root, "alice", PrincipalRole.OWNER)
        admin_review = await request_as(client, "GET", root, "admin", PrincipalRole.ADMIN)
        assert admin_review.status_code == 200
        payload = {"operation_id": str(uuid4()), "fingerprint": review.json()["fingerprint"],
                   "confirmation_name": project["name"], "reason": "approved cleanup"}
        # A preview is scoped to the identity that actually reviewed it.
        assert (await request_as(client, "POST", root, "admin", PrincipalRole.ADMIN,
                                 json=payload)).status_code == 409
        deleted = await request_as(client, "POST", root, "alice", PrincipalRole.OWNER, json=payload)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted", deleted.text
        receipt = f"/api/project-deletions/{project['id']}/{payload['operation_id']}"
        for actor, role in (("bob", PrincipalRole.MEMBER), ("admin", PrincipalRole.ADMIN)):
            response = await request_as(client, "GET", receipt, actor, role)
            assert response.status_code == 403
            assert "Shared private project" not in response.text
        response = await client.get(receipt, headers=signed_headers(
            "GET", receipt, "alice", PrincipalRole.OWNER, session_id="new_gateway_session_alice",
        ))
        assert response.status_code == 403
        response = await request_as(client, "GET", receipt, "alice", PrincipalRole.OWNER)
        assert response.status_code == 200
        assert (await request_as(client, "POST", root, "alice", PrincipalRole.OWNER,
                                 json=payload)).status_code == 200


@pytest.mark.asyncio
async def test_new_app_recovers_before_memory_and_run_recovery_can_recreate_content(
    tmp_path, monkeypatch,
):
    import ophira_backend.project_deletion as module

    settings = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(settings, runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = (await client.post("/api/projects", json={"name": "recover", "objective": "x"}))
        project = created.json()
        path = f"/api/projects/{project['id']}/deletion"
        review = (await client.get(path)).json()
        payload = {"operation_id": str(uuid4()), "fingerprint": review["fingerprint"],
                   "confirmation_name": "recover", "reason": "cleanup"}
        original = module.purge_project_deletion

        def crash(_root, _plan):
            raise OSError("interruption")

        monkeypatch.setattr(module, "purge_project_deletion", crash)
        result = await client.post(path, json=payload)
        assert result.json()["status"] == "blocked", result.text
        assert (await client.get(f"/api/projects/{project['id']}")).status_code == 404
        monkeypatch.setattr(module, "purge_project_deletion", original)
    restarted = create_app(settings, runtime=FakeRuntime())
    operation = restarted.state.project_deletion.get(project["id"], payload["operation_id"], None)
    assert operation.status == "deleted"
    assert not (tmp_path / "projects" / project["id"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["intent", "completion"])
async def test_commit_then_fsync_failure_is_uncertain_and_queries_the_same_operation(
    tmp_path, monkeypatch, failure_stage,
):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    service = app.state.project_deletion
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = await client.post("/api/projects", json={"name": "uncertain", "objective": "x"})
        project = created.json()
        path = f"/api/projects/{project['id']}/deletion"
        review = (await client.get(path)).json()
        payload = {"operation_id": str(uuid4()), "fingerprint": review["fingerprint"],
                   "confirmation_name": "uncertain", "reason": "cleanup"}
        original = service.lifecycle.write_state

        def committed_then_failed(identifier, state):
            original(identifier, state)
            if state["status"] == ("deleting" if failure_stage == "intent" else "deleted"):
                raise OSError("synthetic fsync failure after rename")

        monkeypatch.setattr(service.lifecycle, "write_state", committed_then_failed)
        response = await client.post(path, json=payload)
        assert response.status_code == 503, response.text
        assert response.headers["cache-control"] == "no-store"
        operation = f"/api/project-deletions/{project['id']}/{payload['operation_id']}"
        observed = await client.get(operation)
        assert observed.status_code == 200
        assert observed.json()["status"] == (
            "deleting" if failure_stage == "intent" else "deleted"
        )
        monkeypatch.setattr(service.lifecycle, "write_state", original)
        assert (await client.post(operation + "/retry")).json()["status"] == "deleted"
