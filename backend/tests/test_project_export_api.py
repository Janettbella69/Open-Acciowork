"""Private, reviewed project archives over the actual HTTP/authorization boundary."""

import hashlib
import io
import json
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, signed_headers, trusted_settings

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.workspace_checkpoints import workspace_lease


async def create_local(client):
    response = await client.post(
        "/api/projects", json={"name": "Export review", "objective": "Keep project records"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def assert_private(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_local_export_reviews_then_downloads_matching_private_zip(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        project_id = await create_local(client)
        source = "Supplier,price\n测试,120\n".encode()
        artifact = app.state.ledger.save_upload(project_id, "报价.csv", source)
        base = f"/api/projects/{project_id}/export"
        preview = await client.get(base)
        assert preview.status_code == 200, preview.text
        assert_private(preview)
        view = preview.json()
        assert view["project_id"] == project_id
        assert view["actor_id"] == "local-operator"
        assert view["file_count"] == len(view["entries"])
        assert len(view["fingerprint"]) == 64
        archive = await client.post(base + "/download", json={"fingerprint": view["fingerprint"]})
        assert archive.status_code == 200, archive.text
        assert_private(archive)
        assert archive.headers["content-type"] == "application/zip"
        assert archive.headers["content-disposition"] == (
            f'attachment; filename="{project_id}-export.zip"'
        )
        with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
            raw_files = []
            for entry in view["entries"]:
                raw = package.read(entry["path"])
                assert len(raw) == entry["size_bytes"]
                assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
                raw_files.append(raw)
            assert source in raw_files
            assert any(artifact.id.encode() in raw for raw in raw_files)
            manifest = json.loads(package.read("manifest.json"))
            assert manifest["project_id"] == project_id
            assert manifest["fingerprint"] == view["fingerprint"]
        assert app.state.ledger.list_runs(project_id) == []


@pytest.mark.asyncio
async def test_export_rejects_changed_source_and_requires_new_preview(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        project_id = await create_local(client)
        base = f"/api/projects/{project_id}/export"
        previous = (await client.get(base)).json()
        app.state.ledger.save_upload(project_id, "new.txt", b"A new reference")
        rejected = await client.post(
            base + "/download", json={"fingerprint": previous["fingerprint"]},
        )
        assert rejected.status_code == 409
        assert_private(rejected)
        assert "A new reference" not in rejected.text
        current = (await client.get(base)).json()
        assert current["fingerprint"] != previous["fingerprint"]
        downloaded = await client.post(
            base + "/download", json={"fingerprint": current["fingerprint"]},
        )
        assert downloaded.status_code == 200


@pytest.mark.asyncio
async def test_busy_project_export_returns_retryable_private_conflict(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        project_id = await create_local(client)
        base = f"/api/projects/{project_id}/export"
        with workspace_lease(app.state.ledger, project_id):
            busy = await client.get(base)
        assert busy.status_code == 409
        assert busy.json()["detail"] == "Project files are busy. Try exporting again shortly."
        assert_private(busy)
        assert (await client.get(base)).status_code == 200


@pytest.mark.asyncio
async def test_export_body_validation_and_storage_errors_do_not_echo_private_inputs(
    tmp_path, monkeypatch,
):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        project_id = await create_local(client)
        base = f"/api/projects/{project_id}/export"
        invalid = await client.post(base + "/download", json={"fingerprint": "private-canary"})
        assert invalid.status_code == 422
        assert "private-canary" not in invalid.text
        assert_private(invalid)
        huge = await client.post(base + "/download", content=b"x" * 1025)
        assert huge.status_code == 413
        assert_private(huge)

        def corrupt(*_args):
            raise ValueError("private-canary")

        monkeypatch.setattr(app.state.project_exports, "preview", corrupt)
        broken = await client.get(base)
        assert broken.status_code == 409
        assert "private-canary" not in broken.text
        assert_private(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [PrincipalRole.MEMBER, PrincipalRole.REVIEWER, PrincipalRole.VIEWER],
)
async def test_project_members_cannot_request_full_administrator_archive(tmp_path, role):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="https://gateway.test") as client:
        created = await request_as(
            client, "POST", "/api/projects", "owner", PrincipalRole.OWNER,
            json={"name": "Private project", "objective": "private-canary"},
        )
        project_id = created.json()["id"]
        app.state.project_exports.access.grant(
            project_id, principal("owner", PrincipalRole.OWNER), principal_id="reader", role=role,
        )
        base = f"/api/projects/{project_id}/export"
        for method, path, options in (
            ("GET", base, {}),
            ("POST", base + "/download", {"json": {"fingerprint": "a" * 64}}),
        ):
            denied = await request_as(client, method, path, "reader", role, **options)
            assert denied.status_code == 403
            assert "private-canary" not in denied.text
            assert_private(denied)


@pytest.mark.asyncio
async def test_export_review_is_bound_to_current_actor_session_and_project(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="https://gateway.test") as client:
        projects = []
        for name in ("First", "Second"):
            response = await request_as(
                client, "POST", "/api/projects", "owner", PrincipalRole.OWNER,
                json={"name": name, "objective": "Review"},
            )
            projects.append(response.json()["id"])
        project_id = projects[0]
        app.state.project_exports.access.grant(
            project_id, principal("owner", PrincipalRole.OWNER),
            principal_id="admin", role=PrincipalRole.ADMIN,
        )
        base = f"/api/projects/{project_id}/export"
        preview = await request_as(client, "GET", base, "owner", PrincipalRole.OWNER)
        assert preview.status_code == 200, preview.text
        review = {"fingerprint": preview.json()["fingerprint"]}
        for actor, target in (("admin", project_id), ("owner", projects[1])):
            result = await request_as(
                client, "POST", f"/api/projects/{target}/export/download", actor,
                PrincipalRole.ADMIN if actor == "admin" else PrincipalRole.OWNER, json=review,
            )
            assert result.status_code == 409
            assert_private(result)
        changed_session = await client.post(
            base + "/download", json=review,
            headers=signed_headers(
                "POST", base + "/download", "owner", PrincipalRole.OWNER,
                session_id="another-authenticated-session",
            ),
        )
        assert changed_session.status_code == 409
        allowed = await request_as(
            client, "POST", base + "/download", "owner", PrincipalRole.OWNER, json=review,
        )
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_export_rechecks_authority_after_archive_has_been_built(tmp_path, monkeypatch):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    service = app.state.project_exports
    async with AsyncClient(transport=ASGITransport(app), base_url="https://gateway.test") as client:
        response = await request_as(
            client, "POST", "/api/projects", "owner", PrincipalRole.OWNER,
            json={"name": "Export scope", "objective": "private-canary"},
        )
        project_id = response.json()["id"]
        owner = principal("owner", PrincipalRole.OWNER)
        service.access.grant(project_id, owner, principal_id="admin", role=PrincipalRole.ADMIN)
        base = f"/api/projects/{project_id}/export"
        preview = await request_as(client, "GET", base, "admin", PrincipalRole.ADMIN)
        original = service.download

        def revoked_after_capture(*args):
            package = original(*args)
            service.access.revoke(project_id, owner, principal_id="admin")
            return package

        monkeypatch.setattr(service, "download", revoked_after_capture)
        denied = await request_as(
            client, "POST", base + "/download", "admin", PrincipalRole.ADMIN,
            json={"fingerprint": preview.json()["fingerprint"]},
        )
        assert denied.status_code == 403
        assert "private-canary" not in denied.text
        assert "application/zip" not in denied.headers["content-type"]
        assert_private(denied)
