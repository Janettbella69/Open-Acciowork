import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate


@pytest.mark.asyncio
async def test_knowledge_api_scopes_search_and_validates_query(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    owner = principal("owner", PrincipalRole.OWNER)
    project = ledger.create_project(
        ProjectCreate(name="Sources", objective="Review"), principal=owner
    )
    app.state.project_access.bind_project(project.id, owner)
    ledger.save_upload(project.id, "reference.txt", b"confidential quote")
    path = f"/api/projects/{project.id}/knowledge"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        response = await request_as(
            client, "GET", path + "?q=quote", owner.principal_id, owner.role
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["matches"][0]["excerpt"] == "confidential quote"
        denied = await request_as(client, "GET", path, "outsider", PrincipalRole.OWNER)
        assert denied.status_code == 403 and "confidential" not in denied.text
        unsigned = await client.get(path)
        assert unsigned.status_code == 401
        for query in (
            "limit=0",
            "limit=31",
            "q=" + "x" * 201,
            "q=" + "+".join(map(str, range(17))),
        ):
            invalid = await request_as(
                client, "GET", path + "?" + query, owner.principal_id, owner.role
            )
            assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_after", [1, 2])
async def test_knowledge_rechecks_membership_before_and_after_capture(
    tmp_path, monkeypatch, revoke_after
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    viewer = principal("viewer", PrincipalRole.VIEWER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Sources", objective="Review"),
        principal=owner,
    )
    access = app.state.project_access
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id=viewer.principal_id, role=viewer.role)
    app.state.ledger.save_upload(project.id, "reference.txt", b"confidential quote")
    authorize = access.authorize
    calls = []

    def revoke(*args, **kwargs):
        result = authorize(*args, **kwargs)
        calls.append(True)
        if len(calls) == revoke_after:
            access.revoke(project.id, owner, principal_id=viewer.principal_id)
        return result

    monkeypatch.setattr(access, "authorize", revoke)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        response = await request_as(
            client,
            "GET",
            f"/api/projects/{project.id}/knowledge?q=quote",
            viewer.principal_id,
            viewer.role,
        )
    assert response.status_code == 403
    assert "confidential" not in response.text


@pytest.mark.asyncio
async def test_reference_download_captures_checked_bytes_and_rejects_edits(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Sources", objective="Review"), principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    artifact = app.state.ledger.save_upload(project.id, "quote.txt", b"registered quote")
    path = f"/api/projects/{project.id}/knowledge/{artifact.id}/download"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(client, "GET", path, owner.principal_id, owner.role)
        assert response.status_code == 200 and response.content == b"registered quote"
        assert response.headers["etag"] == f'"{artifact.checksum}"'
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["cache-control"] == "no-store"
        denied = await request_as(client, "GET", path, "outsider", PrincipalRole.OWNER)
        assert denied.status_code == 403
        source = app.state.ledger.workspace_for_project(project.id) / artifact.path
        source.write_text("a different quote")
        conflict = await request_as(client, "GET", path, owner.principal_id, owner.role)
        assert conflict.status_code == 409
        assert "a different quote" not in conflict.text
        source.unlink()
        source.symlink_to(tmp_path / "private-token.txt")
        conflict = await request_as(client, "GET", path, owner.principal_id, owner.role)
        assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_reference_download_rechecks_permission_after_read(tmp_path, monkeypatch):
    import ophira_backend.main as main

    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    viewer = principal("viewer", PrincipalRole.VIEWER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Sources", objective="Review"), principal=owner,
    )
    access = app.state.project_access
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id=viewer.principal_id, role=viewer.role)
    artifact = app.state.ledger.save_upload(project.id, "quote.txt", b"private download")
    original = main.capture_reference

    def revoke_after_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        access.revoke(project.id, owner, principal_id=viewer.principal_id)
        return result

    monkeypatch.setattr(main, "capture_reference", revoke_after_capture)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client, "GET", f"/api/projects/{project.id}/knowledge/{artifact.id}/download",
            viewer.principal_id, viewer.role,
        )
    assert response.status_code == 403
    assert "private download" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
async def test_office_reference_api_keeps_version_and_project_authorization(tmp_path, suffix):
    from test_office_knowledge import office_bytes

    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Office", objective="Review"), principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    raw = office_bytes(tmp_path, suffix)
    artifact = app.state.ledger.save_upload(project.id, "quote" + suffix, raw)
    base = f"/api/projects/{project.id}/knowledge"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        response = await request_as(client, "GET", base + "?q=Acme", owner.principal_id,
                                    owner.role)
        assert response.status_code == 200
        data = response.json()
        assert data["matches"][0]["source_location"]["kind"]
        assert data["documents"][0]["checksum"] == artifact.checksum
        assert data["documents"][0]["extraction_notice"]
        download = f"{base}/{artifact.id}/download"
        result = await request_as(client, "GET", download, owner.principal_id, owner.role)
        assert result.status_code == 200 and result.content == raw
        assert result.headers["etag"] == f'"{artifact.checksum}"'
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.headers["cache-control"] == "no-store"
        for path in (base + "?q=Acme", download):
            denied = await request_as(client, "GET", path, "outsider", PrincipalRole.OWNER)
            assert denied.status_code == 403 and "Acme" not in denied.text
