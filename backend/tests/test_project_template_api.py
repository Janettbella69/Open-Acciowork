"""HTTP routes with actual disk copies and signed organization assertions; no model Run."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_project_templates import seed

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app


@pytest.mark.asyncio
async def test_signed_template_http_flow_scopes_files_and_rechecks_current_access(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner, member, viewer = "owner", "curator", "reader"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await request_as(
            client,
            "POST",
            "/api/projects",
            owner,
            PrincipalRole.OWNER,
            json={"name": "Source", "objective": "Private source"},
        )
        assert created.status_code == 201
        project_id = created.json()["id"]
        access = app.state.project_templates.access
        for user, role in [(member, PrincipalRole.MEMBER), (viewer, PrincipalRole.VIEWER)]:
            access.grant(
                project_id, principal(owner, PrincipalRole.OWNER), principal_id=user, role=role
            )
        artifact = app.state.ledger.save_upload(project_id, "报价.txt", "原始报价".encode())
        base = f"/api/projects/{project_id}/templates"
        payload = {
            "request_id": uuid4().hex,
            "title": "Supplier review",
            "seed": seed().model_dump(),
            "artifact_ids": [artifact.id],
        }
        response = await request_as(
            client, "POST", base, member, PrincipalRole.MEMBER, json=payload
        )
        assert response.status_code == 200, response.text
        template = response.json()
        path = base + "/" + template["id"]
        detail = await request_as(client, "GET", path + "/versions/1", viewer, PrincipalRole.VIEWER)
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        download = path + f"/versions/1/references/{artifact.id}/download"
        result = await request_as(client, "GET", download, viewer, PrincipalRole.VIEWER)
        assert result.content == "原始报价".encode()
        assert result.headers["x-content-type-options"] == "nosniff"
        assert "filename*=UTF-8''" in result.headers["content-disposition"]
        apply = {
            "request_id": uuid4().hex,
            "version": 1,
            "content_hash": template["content_hash"],
            "seed": seed("Fresh signed project").model_dump(),
            "artifact_ids": [artifact.id],
        }
        denied = await request_as(
            client, "POST", path + "/applications", member, PrincipalRole.MEMBER, json=apply
        )
        assert denied.status_code == 403
        made = await request_as(
            client, "POST", path + "/applications", owner, PrincipalRole.OWNER, json=apply
        )
        assert made.status_code == 200, made.text
        project, application = made.json()["project"], made.json()["application"]
        repeated = await request_as(
            client, "POST", path + "/applications", owner, PrincipalRole.OWNER, json=apply
        )
        assert repeated.json()["project"]["id"] == project["id"]
        assert app.state.ledger.list_runs(project["id"]) == []
        assert set(access.get(project["id"]).memberships) == {owner}
        resume = path + "/applications/" + application["id"] + "/resume"
        invalid = await request_as(
            client, "POST", resume, owner, PrincipalRole.OWNER, json={"confirm_creation": False}
        )
        assert invalid.status_code == 422
        resumed = await request_as(
            client, "POST", resume, owner, PrincipalRole.OWNER, json={"confirm_creation": True}
        )
        assert resumed.json()["project"]["id"] == project["id"]
        access.revoke(project_id, principal(owner, PrincipalRole.OWNER), principal_id=viewer)
        for endpoint in [base, path + "/versions/1", download, path + "/applications"]:
            forbidden = await request_as(client, "GET", endpoint, viewer, PrincipalRole.VIEWER)
            assert forbidden.status_code == 403 and forbidden.headers["cache-control"] == "no-store"
            assert "原始报价" not in forbidden.text


@pytest.mark.asyncio
async def test_local_template_http_validation_never_echoes_sensitive_drafts(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        project = (
            await client.post("/api/projects", json={"name": "Local", "objective": "Review"})
        ).json()
        base = f"/api/projects/{project['id']}/templates"
        invalid = await client.post(
            base,
            json={
                "request_id": uuid4().hex,
                "title": "token=private-canary",
                "seed": {"evidence": "private-canary"},
            },
        )
        assert invalid.status_code == 422 and "private-canary" not in invalid.text
        huge = await client.post(base, content=b"x" * (256 * 1024 + 1))
        assert huge.status_code == 413 and huge.headers["cache-control"] == "no-store"
        payload = {"request_id": uuid4().hex, "title": "Reusable", "seed": seed().model_dump()}
        template = (await client.post(base, json=payload)).json()
        path = base + "/" + template["id"]
        new_version = {
            **payload,
            "request_id": uuid4().hex,
            "expected_revision": 1,
            "title": "Updated",
        }
        updated = await client.post(path + "/versions", json=new_version)
        assert updated.status_code == 200 and updated.json()["latest_version"] == 2
        stale = await client.post(
            path + "/versions", json={**new_version, "request_id": uuid4().hex}
        )
        assert stale.status_code == 409
        archive = await client.post(
            path + "/archive",
            json={"request_id": uuid4().hex, "expected_revision": 2, "archived": True},
        )
        assert archive.status_code == 200 and archive.json()["archived"]
        assert (await client.get(path + "/versions/1")).json()["version"]["title"] == "Reusable"
        assert (await client.get(path + "/applications?limit=101")).status_code == 422
