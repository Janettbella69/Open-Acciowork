from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_conversation_deletion import setup

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.main import create_app


@pytest.mark.asyncio
async def test_conversation_http_erasure_preserves_project_and_other_run(tmp_path):
    _ledger, _service, project, first, resumed, other, folder = setup(tmp_path)
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    path = f"/api/projects/{project.id}/conversations/{first.id}/deletion"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        review = await client.get(path)
        assert review.status_code == 200, review.text
        assert review.headers["cache-control"] == "no-store"
        assert set(review.json()["run_ids"]) == {first.id, resumed.id}
        payload = {
            "operation_id": str(uuid4()),
            "fingerprint": review.json()["fingerprint"],
            "confirmation_name": project.name,
            "reason": "fixture deletion",
        }
        deleted = await client.post(path, json=payload)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted", deleted.text
        assert not (folder / f"{first.sdk_session_id}.jsonl").exists()
        assert (folder / f"{other.sdk_session_id}.jsonl").exists()
        assert (await client.get(f"/api/projects/{project.id}")).status_code == 200
        assert (await client.get(f"/api/runs/{first.id}")).status_code == 404
        assert (await client.get(f"/api/runs/{other.id}")).status_code == 200
        operation = f"/api/conversation-deletions/{project.id}/{payload['operation_id']}"
        for method, route in (("GET", operation), ("POST", operation + "/retry"), ("POST", path)):
            response = await client.request(method, route, json=payload if route == path else None)
            assert response.status_code == 200, response.text
            assert response.json() == deleted.json()
            assert response.headers["cache-control"] == "no-store"
        assert (
            await client.post(f"/api/runs/{first.id}/retry", json={"strategy": "resume"})
        ).status_code == 404


@pytest.mark.asyncio
async def test_conversation_owner_review_and_session_bound_status(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("alice", PrincipalRole.OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        created = await request_as(
            client,
            "POST",
            "/api/projects",
            "alice",
            PrincipalRole.OWNER,
            json={"name": "private project", "objective": "fixture"},
        )
        project = created.json()
        app.state.project_access.grant(
            project["id"], owner, principal_id="bob", role=PrincipalRole.MEMBER
        )
        source = app.state.ledger.create_run(project["id"], "private prompt", principal=owner)
        app.state.ledger.update_run(source.id, status=RunStatus.COMPLETED)
        path = f"/api/projects/{project['id']}/conversations/{source.id}/deletion"
        assert (
            await request_as(client, "GET", path, "bob", PrincipalRole.MEMBER)
        ).status_code == 403
        review = await request_as(client, "GET", path, "alice", PrincipalRole.OWNER)
        assert review.status_code == 200, review.text
        payload = {
            "operation_id": str(uuid4()),
            "fingerprint": review.json()["fingerprint"],
            "confirmation_name": project["name"],
            "reason": "fixture cleanup",
        }
        deleted = await request_as(client, "POST", path, "alice", PrincipalRole.OWNER, json=payload)
        assert deleted.status_code == 200 and deleted.json()["status"] == "deleted", deleted.text
        operation = f"/api/conversation-deletions/{project['id']}/{payload['operation_id']}"
        forbidden = await request_as(client, "GET", operation, "bob", PrincipalRole.MEMBER)
        assert forbidden.status_code == 403
        assert "private project" not in forbidden.text and "private prompt" not in forbidden.text


@pytest.mark.asyncio
async def test_conversation_recovery_before_store_startup(tmp_path, monkeypatch):
    import ophira_backend.conversation_deletion as module

    _ledger, _service, project, source, *_ = setup(tmp_path)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(settings, runtime=FakeRuntime())
    path = f"/api/projects/{project.id}/conversations/{source.id}/deletion"
    original = module.purge_conversation

    def interrupted(*_args):
        raise OSError("synthetic interruption")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        review = (await client.get(path)).json()
        payload = {
            "operation_id": str(uuid4()),
            "fingerprint": review["fingerprint"],
            "confirmation_name": project.name,
            "reason": "fixture cleanup",
        }
        monkeypatch.setattr(module, "purge_conversation", interrupted)
        blocked = await client.post(path, json=payload)
        assert blocked.json()["status"] == "blocked", blocked.text
        assert (await client.get(f"/api/projects/{project.id}")).status_code == 404
        monkeypatch.setattr(module, "purge_conversation", original)
    restarted = create_app(settings, runtime=FakeRuntime())
    assert (
        restarted.state.conversation_deletion.get(project.id, payload["operation_id"], None).status
        == "deleted"
    )
    assert restarted.state.ledger.get_project(project.id).id == project.id
