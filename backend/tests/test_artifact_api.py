from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate


@pytest.fixture
def source(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Files", objective="Edit"), principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    artifact = app.state.ledger.save_upload(project.id, "report.md", b"Initial report")
    return app, owner, project, artifact


@pytest.mark.asyncio
async def test_version_edit_download_and_knowledge_share_real_persisted_bytes(source):
    app, owner, project, first = source
    base = f"/api/projects/{project.id}/artifacts"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        preview = await request_as(
            client, "GET", f"{base}/{first.id}/text", owner.principal_id, owner.role,
        )
        assert preview.status_code == 200 and preview.json()["editable"]
        assert preview.headers["cache-control"] == "no-store"
        payload = {
            "content": "Updated supplier brief", "expected_checksum": first.checksum,
            "request_id": "edit_1",
        }
        response = await request_as(
            client, "PUT", f"{base}/{first.id}/text", owner.principal_id, owner.role, json=payload,
        )
        assert response.status_code == 200
        second = response.json()
        assert second["version"] == 2 and second["created_by"] == owner.principal_id
        retry = await request_as(
            client, "PUT", f"{base}/{first.id}/text", owner.principal_id, owner.role, json=payload,
        )
        assert retry.json()["id"] == second["id"]
        old = await request_as(
            client, "GET", f"{base}/{first.id}/download", owner.principal_id, owner.role,
        )
        latest = await request_as(
            client, "GET", f"{base}/{second['id']}/download", owner.principal_id, owner.role,
        )
        assert old.content == b"Initial report"
        assert latest.content == b"Updated supplier brief"
        assert old.headers["etag"] == f'"{first.checksum}"'
        assert old.headers["cache-control"] == "no-store"
        assert old.headers["x-content-type-options"] == "nosniff"
        assert old.headers["content-disposition"].startswith("attachment;")
        knowledge = await request_as(
            client, "GET", f"/api/projects/{project.id}/knowledge?q=supplier",
            owner.principal_id, owner.role,
        )
        assert knowledge.json()["matches"][0]["artifact_id"] == second["id"]
        stale = await request_as(
            client, "PUT", f"{base}/{first.id}/text", owner.principal_id, owner.role,
            json={**payload, "content": "Overwriting old version", "request_id": "edit_2"},
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_viewer_can_read_but_cannot_edit_and_outsider_cannot_download(source):
    app, owner, project, artifact = source
    viewer = principal("viewer", PrincipalRole.VIEWER)
    app.state.project_access.grant(
        project.id, owner, principal_id=viewer.principal_id, role=viewer.role,
    )
    base = f"/api/projects/{project.id}/artifacts/{artifact.id}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        view = await request_as(client, "GET", base + "/text", viewer.principal_id, viewer.role)
        assert view.status_code == 200 and not view.json()["editable"]
        denied = await request_as(
            client, "PUT", base + "/text", viewer.principal_id, viewer.role,
            json={
                "content": "Overwrite", "expected_checksum": artifact.checksum, "request_id": "x",
            },
        )
        assert denied.status_code == 403
        for suffix in ("/text", "/download"):
            other = await request_as(client, "GET", base + suffix, "outsider", PrincipalRole.OWNER)
            assert other.status_code == 403 and "Initial report" not in other.text
        unsigned = await client.get(base + "/download")
        assert unsigned.status_code == 401


@pytest.mark.asyncio
async def test_download_rechecks_authorization_after_capturing_bytes(source, monkeypatch):
    app, owner, project, artifact = source
    viewer = principal("viewer", PrincipalRole.VIEWER)
    access = app.state.project_access
    access.grant(project.id, owner, principal_id=viewer.principal_id, role=viewer.role)
    original = app.state.ledger.capture_artifact

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        access.revoke(project.id, owner, principal_id=viewer.principal_id)
        return result

    monkeypatch.setattr(app.state.ledger, "capture_artifact", capture)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client, "GET", f"/api/projects/{project.id}/artifacts/{artifact.id}/download",
            viewer.principal_id, viewer.role,
        )
    assert response.status_code == 403 and "Initial report" not in response.text


@pytest.mark.asyncio
async def test_edit_rechecks_authorization_after_http_body_read(source):
    app, owner, project, artifact = source
    member = principal("member", PrincipalRole.MEMBER)
    access = app.state.project_access
    access.grant(project.id, owner, principal_id=member.principal_id, role=member.role)

    async def body():
        access.revoke(project.id, owner, principal_id=member.principal_id)
        yield (
            '{"content":"overwrite","expected_checksum":"' + artifact.checksum
            + '","request_id":"edit_1"}'
        ).encode()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client, "PUT", f"/api/projects/{project.id}/artifacts/{artifact.id}/text",
            member.principal_id, member.role, content=body(),
        )
    assert response.status_code == 403
    assert len(app.state.ledger.get_project(project.id).artifacts) == 1


@pytest.mark.asyncio
async def test_download_is_the_captured_version_even_after_file_replacement(source, monkeypatch):
    app, owner, project, artifact = source
    original = app.state.ledger.capture_artifact

    def replace_after_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        path = app.state.ledger.workspace_for_project(project.id) / artifact.path
        path.write_bytes(b"replaced-after-capture")
        return result

    monkeypatch.setattr(app.state.ledger, "capture_artifact", replace_after_capture)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client, "GET", f"/api/projects/{project.id}/artifacts/{artifact.id}/download",
            owner.principal_id, owner.role,
        )
    assert response.status_code == 200 and response.content == b"Initial report"
