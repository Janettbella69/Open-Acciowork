from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from ophira_backend.auth import ProjectAccessStore, build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate
from ophira_backend.project_deletion import ProjectDeletionService
from ophira_backend.project_deletion_models import ProjectDeletionRequest
from ophira_backend.site_bundle import InvalidSiteBundle, read_site_bundle
from ophira_backend.site_publications import SitePublicationDecision, SitePublicationStore
from ophira_backend.storage import FileLedger

GATEWAY_SECRET = "site-publication-gateway-signing-secret-0001"
HTML = (
    b"<!doctype html><html><head><style>h1{color:green}</style></head>"
    b"<body><h1>Shop</h1><script>window.siteScriptRan=true</script></body></html>"
)
SITE_HTML = (
    b'<!doctype html><html><head><link rel="stylesheet" href="assets/site.css"></head>'
    b'<body><img src="assets/logo.png" alt="Logo"><h1>Bundle</h1>'
    b'<script src="assets/site.js"></script></body></html>'
)
SITE_CSS = b"h1 { color: rgb(0, 128, 0); }"
SITE_JS = b'document.body.dataset.script="ran";'
SITE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f"
    b"\x00\x03\x03\x02\x00\xef\xbf\xca\xa9\x00\x00\x00\x00IEND\xaeB`\x82"
)


def site_zip(entries: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in (entries or {
            "index.html": SITE_HTML,
            "assets/site.css": SITE_CSS,
            "assets/site.js": SITE_JS,
            "assets/logo.png": SITE_PNG,
        }).items():
            archive.writestr(name, raw)
    return output.getvalue()


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def gateway_headers(
    method: str, path: str, role: PrincipalRole = PrincipalRole.OWNER,
    principal_id: str = "owner@example.com",
):
    return build_gateway_assertion_headers(
        GATEWAY_SECRET, method=method, path=path,
        tenant_id="tenant_sites", principal_id=principal_id, role=role,
        session_id="gateway_session_sites_owner", issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_reviewed_site_publication_is_pinned_public_and_revocable(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=GATEWAY_SECRET,
        trusted_gateway_tenant_id="tenant_sites",
        site_public_origin="https://pages.example.com",
    )
    app = create_app(settings, runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.com",
    ) as client:
        async def managed(method: str, path: str, **kwargs):
            return await client.request(
                method, path,
                headers=gateway_headers(method, path.split("?", 1)[0]), **kwargs,
            )

        created = await managed(
            "POST", "/api/projects", json={"name": "Shop", "objective": "Landing page"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        access_path = f"/api/projects/{project_id}/access"
        for principal_id, role in (
            ("member@example.com", PrincipalRole.MEMBER),
            ("viewer@example.com", PrincipalRole.VIEWER),
        ):
            granted = await managed(
                "POST", access_path, json={"principal_id": principal_id, "role": role.value},
            )
            assert granted.status_code == 200, granted.text
        workspace = app.state.ledger.workspace_for_project(project_id)
        (workspace / "notes.txt").write_text("Private notes")
        notes = app.state.ledger.register_artifact(project_id, "notes.txt")
        (workspace / "landing.html").write_bytes(HTML)
        artifact = app.state.ledger.register_artifact(project_id, "landing.html")
        base = f"/api/projects/{project_id}/site-publications"
        member_list = await client.get(
            base, headers=gateway_headers(
                "GET", base, PrincipalRole.MEMBER, "member@example.com",
            ),
        )
        assert member_list.status_code == 200
        assert member_list.json()["can_stage"] is True
        assert member_list.json()["can_publish"] is False
        viewer_list = await client.get(
            base, headers=gateway_headers(
                "GET", base, PrincipalRole.VIEWER, "viewer@example.com",
            ),
        )
        assert viewer_list.status_code == 200
        assert viewer_list.json()["can_stage"] is False
        assert viewer_list.json()["can_publish"] is False
        viewer_stage = await client.post(
            base, json={"artifact_id": artifact.id},
            headers=gateway_headers(
                "POST", base, PrincipalRole.VIEWER, "viewer@example.com",
            ),
        )
        assert viewer_stage.status_code == 403
        refused = await managed("POST", base, json={"artifact_id": notes.id})
        assert refused.status_code == 409
        staged = await managed("POST", base, json={"artifact_id": artifact.id})
        assert staged.status_code == 201, staged.text
        record = staged.json()["publication"]
        assert record["state"] == "draft"
        assert record["artifact_checksum"] == artifact.checksum
        site_id = record["id"]
        public_path = f"/published/{project_id}/{site_id}"
        public_url = f"https://pages.example.com{public_path}"
        assert staged.json()["public_url"] == public_url
        unpublished = await client.get(public_url)
        assert unpublished.status_code == 404
        assert unpublished.headers["cache-control"].startswith("no-store")

        preview = await managed("GET", f"{base}/{site_id}/preview")
        assert preview.status_code == 200
        assert preview.content == HTML
        assert "sandbox;" in preview.headers["content-security-policy"]
        assert "frame-ancestors 'self'" in preview.headers["content-security-policy"]
        wrong_hash = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": "0" * 64,
            "confirmation": "publish",
        })
        assert wrong_hash.status_code == 409
        member_publish_path = f"{base}/{site_id}/publish"
        member_publish = await client.post(
            member_publish_path,
            json={
                "expected_revision": 1, "expected_checksum": artifact.checksum,
                "confirmation": "publish",
            },
            headers=gateway_headers(
                "POST", member_publish_path, PrincipalRole.MEMBER, "member@example.com",
            ),
        )
        assert member_publish.status_code == 403
        approved = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": artifact.checksum,
            "confirmation": "publish",
        })
        assert approved.status_code == 200, approved.text
        assert approved.json()["publication"]["state"] == "published"
        assert approved.json()["publication"]["published_by"] == "owner@example.com"
        page = await client.get(public_url)
        assert page.status_code == 200, page.text
        assert page.content == HTML
        assert page.headers["cache-control"].startswith("no-store")
        assert "script-src 'none'" in page.headers["content-security-policy"]
        assert "form-action 'none'" in page.headers["content-security-policy"]
        assert (await client.get(f"https://gateway.example.com{public_path}")).status_code == 404
        assert (await client.get("https://pages.example.com/api/projects")).status_code == 404

        (workspace / "landing.html").write_text("<h1>New version</h1>")
        next_version = app.state.ledger.register_artifact(project_id, "landing.html")
        assert next_version.checksum != artifact.checksum
        assert (await client.get(public_url)).content == HTML
        repeated = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": artifact.checksum,
            "confirmation": "publish",
        })
        assert repeated.status_code == 409
        listed = await managed("GET", base)
        assert listed.status_code == 200
        assert listed.json()["can_stage"] is True
        assert listed.json()["can_publish"] is True
        assert listed.json()["publications"][0]["publication"]["state"] == "published"
        revoked = await managed("POST", f"{base}/{site_id}/revoke", json={
            "expected_revision": 2, "confirmation": "revoke",
        })
        assert revoked.status_code == 200
        assert revoked.json()["publication"]["state"] == "revoked"
        assert (await client.get(public_url)).status_code == 404
        assert (await client.get(public_url)).headers["cache-control"].startswith("no-store")


@pytest.mark.asyncio
async def test_site_publication_requires_html_and_public_origin(tmp_path: Path):
    with pytest.raises(ValueError, match="site_public_origin"):
        Settings(data_dir=tmp_path, site_public_origin="http://pages.example.com")
    with pytest.raises(ValueError, match="separate host"):
        Settings(
            data_dir=tmp_path, site_public_origin="https://pages.example.com",
            event_public_origin="https://pages.example.com",
        )


def test_project_deletion_withdraws_owned_publication(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Shop", objective="Landing page"))
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "landing.html").write_bytes(HTML)
    artifact = ledger.register_artifact(project.id, "landing.html")
    sites = SitePublicationStore(ledger, "https://pages.example.com")
    draft = sites.stage(project.id, artifact.id, created_by="local-operator")
    published = sites.publish(
        project.id, draft.publication.id,
        SitePublicationDecision(
            expected_revision=1, expected_checksum=artifact.checksum,
            confirmation="publish",
        ), approved_by="local-operator",
    )
    assert sites.public_html(project.id, published.publication.id) == (HTML, False, False)
    deletion = ProjectDeletionService(ledger, ProjectAccessStore(tmp_path / "auth" / "projects"))
    preview = deletion.preview(project.id, None)
    assert preview.can_delete
    result = deletion.start(project.id, ProjectDeletionRequest(
        operation_id=str(uuid4()), fingerprint=preview.fingerprint,
        confirmation_name=project.name, reason="Delete site and project",
    ), None)
    assert result.status == "deleted"
    assert not (tmp_path / "projects" / project.id).exists()


@pytest.mark.asyncio
async def test_interactive_page_requires_explicit_script_review(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=GATEWAY_SECRET,
        trusted_gateway_tenant_id="tenant_sites",
        site_public_origin="https://pages.example.com",
    )
    app = create_app(settings, runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.com",
    ) as client:
        async def managed(method: str, path: str, **kwargs):
            return await client.request(
                method, path, headers=gateway_headers(method, path), **kwargs,
            )

        created = await managed(
            "POST", "/api/projects", json={"name": "Interactive", "objective": "Page"},
        )
        project_id = created.json()["id"]
        workspace = app.state.ledger.workspace_for_project(project_id)
        (workspace / "landing.html").write_bytes(HTML)
        artifact = app.state.ledger.register_artifact(project_id, "landing.html")
        base = f"/api/projects/{project_id}/site-publications"
        staged = await managed("POST", base, json={
            "artifact_id": artifact.id, "allow_scripts": True,
        })
        assert staged.status_code == 201, staged.text
        record = staged.json()["publication"]
        assert record["allow_scripts"] is True
        site_id = record["id"]
        preview = await managed("GET", f"{base}/{site_id}/preview")
        assert "sandbox allow-scripts" in preview.headers["content-security-policy"]
        assert "script-src 'unsafe-inline'" in preview.headers["content-security-policy"]
        assert "connect-src 'none'" in preview.headers["content-security-policy"]
        assert "form-action 'none'" in preview.headers["content-security-policy"]
        assert "allow-same-origin" not in preview.headers["content-security-policy"]
        rejected = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": artifact.checksum,
            "confirmation": "publish",
        })
        assert rejected.status_code == 409
        approved = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": artifact.checksum,
            "expected_allow_scripts": True, "confirmation": "publish",
        })
        assert approved.status_code == 200, approved.text
        page = await client.get(approved.json()["public_url"])
        assert page.status_code == 200
        assert page.content == HTML
        assert "sandbox allow-scripts" in page.headers["content-security-policy"]
        assert "script-src 'unsafe-inline'" in page.headers["content-security-policy"]
        assert "allow-same-origin" not in page.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_site_zip_assets_are_pinned_previewed_published_and_revoked(tmp_path: Path):
    app = create_app(Settings(
        data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=GATEWAY_SECRET,
        trusted_gateway_tenant_id="tenant_sites",
        site_public_origin="https://pages.example.com",
    ), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.com",
    ) as client:
        async def managed(method: str, path: str, **kwargs):
            return await client.request(
                method, path,
                headers=gateway_headers(method, path.split("?", 1)[0]), **kwargs,
            )

        created = await managed("POST", "/api/projects", json={
            "name": "Site ZIP", "objective": "Bundled site",
        })
        project_id = created.json()["id"]
        uploaded = await managed(
            "POST", f"/api/projects/{project_id}/files?filename=site.zip",
            content=site_zip(),
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact = uploaded.json()
        base = f"/api/projects/{project_id}/site-publications"
        staged = await managed("POST", base, json={
            "artifact_id": artifact["id"], "allow_scripts": True,
        })
        assert staged.status_code == 201, staged.text
        item = staged.json()["publication"]
        assert item["bundle"] is True
        site_id = item["id"]
        public_url = staged.json()["public_url"]
        assert public_url.endswith(f"/{site_id}/")
        assert (await client.get(public_url + "assets/site.css")).status_code == 404
        assert (await client.get(public_url + "assets/site.css")).headers[
            "cache-control"
        ].startswith("no-store")
        assert (await managed("GET", f"{base}/{site_id}/preview")).status_code == 404
        preview_path = f"{base}/{site_id}/preview/"
        preview = await managed("GET", preview_path)
        assert preview.status_code == 200
        assert preview.content == SITE_HTML
        assert "preview/assets/" in preview.headers["content-security-policy"]
        draft_css = await managed("GET", preview_path + "assets/site.css")
        assert draft_css.content == SITE_CSS
        assert draft_css.headers["content-type"].startswith("text/css")
        draft_js = await managed("GET", preview_path + "assets/site.js")
        assert draft_js.content == SITE_JS
        assert draft_js.headers["content-type"].startswith("text/javascript")
        draft_png = await managed("GET", preview_path + "assets/logo.png")
        assert draft_png.content == SITE_PNG
        assert (await managed("GET", preview_path + "assets/missing.css")).status_code == 404
        approved = await managed("POST", f"{base}/{site_id}/publish", json={
            "expected_revision": 1, "expected_checksum": artifact["checksum"],
            "expected_allow_scripts": True, "confirmation": "publish",
        })
        assert approved.status_code == 200, approved.text
        page = await client.get(public_url)
        assert page.status_code == 200
        assert page.content == SITE_HTML
        assert f"/{site_id}/assets/" in page.headers["content-security-policy"]
        assert (await client.get(public_url.rstrip("/"))).status_code == 404
        css = await client.get(public_url + "assets/site.css")
        assert css.content == SITE_CSS
        assert css.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(public_url + "assets/site.js")).content == SITE_JS
        assert (await client.get(public_url + "assets/logo.png")).content == SITE_PNG
        assert (await client.get(public_url + "assets/missing.css")).status_code == 404
        assert (await client.get(
            f"https://gateway.example.com/published/{project_id}/{site_id}/assets/site.css",
        )).status_code == 404
        next_upload = await managed(
            "POST", f"/api/projects/{project_id}/files?filename=site.zip",
            content=site_zip({"index.html": b"<h1>Changed</h1>"}),
        )
        assert next_upload.status_code == 201
        assert next_upload.json()["checksum"] != artifact["checksum"]
        assert (await client.get(public_url + "assets/site.css")).content == SITE_CSS
        revoked = await managed("POST", f"{base}/{site_id}/revoke", json={
            "expected_revision": 2, "confirmation": "revoke",
        })
        assert revoked.status_code == 200
        assert (await client.get(public_url + "assets/site.css")).status_code == 404


@pytest.mark.parametrize("entries", [
    {"index.html": SITE_HTML, "assets/../bad.css": SITE_CSS},
    {"index.html": SITE_HTML, "assets/program.exe": b"alert(1)"},
    {"index.html": SITE_HTML, "assets/logo.png": b"not a PNG"},
    {"other.html": SITE_HTML},
])
def test_site_zip_rejects_unsafe_entries(entries: dict[str, bytes]):
    with pytest.raises(InvalidSiteBundle):
        read_site_bundle(site_zip(entries))
