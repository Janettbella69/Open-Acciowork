"""Independent erasure of project shop browser login profiles."""

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_shop_browser import create_shop
from test_shop_browser import setup as shop_setup

from ophira_backend import web_tools
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import new_id
from ophira_backend.shop_browser_host import session_lease
from ophira_backend.shop_browser_store import ShopBrowserError

setup = shop_setup


async def test_delete_login_removes_all_generations_and_session_but_keeps_shop(setup):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    directory = service.store.directory(shop["id"])
    for generation in (1, 3):
        profile = directory / f"profile-{generation}" / "Default"
        profile.mkdir(parents=True)
        (profile / "Cookies").write_text("private-cookie-secret")
    (directory / "session.json").write_text("private-session-token")
    before_project = service.ledger.get_project(project.id)
    assert service.store.has_login_files(shop["id"])

    archive = service.export_login(project.id, shop["id"], shop["revision"], None)
    try:
        with zipfile.ZipFile(archive) as saved:
            assert saved.read("profile-1/Default/Cookies") == b"private-cookie-secret"
            assert saved.read("profile-3/Default/Cookies") == b"private-cookie-secret"
            assert "session.json" not in saved.namelist()
    finally:
        archive.close()
    assert service.store.has_login_files(shop["id"])

    deleted = service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert deleted["browser_profile_present"] is False
    assert deleted["profile_generation"] == shop["profile_generation"] + 1
    assert deleted["login_deletion_count"] == 1
    assert deleted["last_login_deleted_generation"] == shop["profile_generation"]
    assert deleted["revision"] == shop["revision"] + 1
    assert deleted["authorized_session_id"] is None
    assert deleted["agent_access"] is False
    assert not (directory / "profile-1").exists()
    assert not (directory / "profile-3").exists()
    assert not (directory / "session.json").exists()
    assert service.ledger.get_project(project.id) == before_project
    assert service.store.get(shop["id"]).id == shop["id"]
    with pytest.raises(ShopBrowserError) as stale:
        service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert stale.value.code == "SHOP_CONFLICT"


async def test_delete_login_rejects_open_browser_and_profile_symlink(setup, tmp_path):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    directory = service.store.directory(shop["id"])
    profile = directory / "profile-1"
    profile.mkdir()
    (profile / "Cookies").write_text("private-cookie-secret")
    with session_lease(service.store, shop["id"]):
        with pytest.raises(ShopBrowserError) as open_browser:
            service.delete_login(project.id, shop["id"], shop["revision"], None)
        with pytest.raises(ShopBrowserError) as export_open:
            service.export_login(project.id, shop["id"], shop["revision"], None)
    assert open_browser.value.code == "SHOP_BROWSER_OPEN"
    assert export_open.value.code == "SHOP_BROWSER_OPEN"
    assert (profile / "Cookies").exists()
    assert service.store.get(shop["id"]).revision == shop["revision"]

    (profile / "Cookies").unlink()
    profile.rmdir()
    outside = tmp_path / "other-profile"
    outside.mkdir()
    (outside / "Cookies").write_text("outside-secret")
    profile.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ShopBrowserError) as export_invalid:
        service.export_login(project.id, shop["id"], shop["revision"], None)
    assert export_invalid.value.code == "SHOP_PROFILE_INVALID"
    with pytest.raises(ShopBrowserError) as invalid:
        service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert invalid.value.code == "SHOP_PROFILE_INVALID"
    assert (outside / "Cookies").read_text() == "outside-secret"
    assert service.store.get(shop["id"]).revision == shop["revision"]


async def test_session_only_data_can_be_deleted_but_not_exported(setup):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    directory = service.store.directory(shop["id"])
    (directory / "session.json").write_text(json.dumps({
        "id": new_id("shop_session"), "shop_id": shop["id"],
        "state": "failed", "token": "x" * 32,
    }))
    view = service.view(project.id, shop["id"])
    assert view["browser_login_data_present"] is True
    assert view["browser_profile_present"] is False
    with pytest.raises(ShopBrowserError) as missing:
        service.export_login(project.id, shop["id"], shop["revision"], None)
    assert missing.value.code == "SHOP_PROFILE_MISSING"
    deleted = service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert deleted["browser_login_data_present"] is False
    assert not (directory / "session.json").exists()


async def test_export_rejects_linked_profile_file(setup, tmp_path):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    profile = service.store.directory(shop["id"]) / "profile-1"
    profile.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("outside-secret")
    (profile / "Cookies").symlink_to(outside)
    with pytest.raises(ShopBrowserError) as invalid:
        service.export_login(project.id, shop["id"], shop["revision"], None)
    assert invalid.value.code == "SHOP_PROFILE_INVALID"
    assert outside.read_text() == "outside-secret"


async def test_interrupted_login_deletion_remains_revoked_and_can_resume(setup, monkeypatch):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    directory = service.store.directory(shop["id"])
    for generation in (1, 2):
        profile = directory / f"profile-{generation}"
        profile.mkdir()
        (profile / "Cookies").write_text("private-cookie-secret")
    from ophira_backend.shop_browser import shutil

    original = shutil.rmtree

    def fail_second(path):
        if path.name == "profile-2":
            raise OSError("synthetic interrupted deletion")
        return original(path)

    fail_second.avoids_symlink_attacks = original.avoids_symlink_attacks
    monkeypatch.setattr(shutil, "rmtree", fail_second)
    with pytest.raises(ShopBrowserError) as interrupted:
        service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert interrupted.value.code == "SHOP_LOGIN_DELETE_FAILED"
    persisted = service.store.get(shop["id"])
    assert persisted.revision == shop["revision"] + 1
    assert persisted.profile_generation == shop["profile_generation"] + 1
    assert persisted.login_deletion_count == 1
    assert persisted.last_login_deleted_generation == shop["profile_generation"]
    assert persisted.authorized_session_id is None
    assert not (directory / "profile-1").exists()
    assert (directory / "profile-2").exists()
    monkeypatch.setattr(shutil, "rmtree", original)
    retried = service.delete_login(project.id, shop["id"], persisted.revision, None)
    assert retried["browser_profile_present"] is False
    assert retried["login_deletion_count"] == 2
    assert not (directory / "profile-2").exists()


@pytest.mark.asyncio
async def test_login_delete_api_requires_project_admin_and_explicit_confirmation(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _host: ["93.184.216.34"])
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://gateway.test",
    ) as client:
        project = await request_as(
            client, "POST", "/api/projects", "owner", PrincipalRole.OWNER,
            json={"name": "Shop", "objective": "Review listings"},
        )
        project_id = project.json()["id"]
        app.state.shop_browsers.authority.access.grant(
            project_id, principal("owner", PrincipalRole.OWNER),
            principal_id="member", role=PrincipalRole.MEMBER,
        )
        base = f"/api/projects/{project_id}/shops"
        created = await request_as(
            client, "POST", base, "owner", PrincipalRole.OWNER,
            json={
                "name": "My shop", "seller_shop_id": "seller-1",
                "platform": "other", "entry_url": "https://seller.example.com/",
            },
        )
        assert created.status_code == 200, created.text
        shop = created.json()
        profile = app.state.shop_browsers.store.directory(shop["id"]) / "profile-1"
        profile.mkdir()
        (profile / "Cookies").write_text("private-cookie-secret")
        path = f"{base}/{shop['id']}/login/delete"
        payload = {"expected_revision": shop["revision"], "confirmation": "delete"}
        method_denied = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert method_denied.status_code == 405
        denied = await request_as(
            client, "POST", path, "member", PrincipalRole.MEMBER, json=payload,
        )
        assert denied.status_code == 403
        invalid = await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
            json={**payload, "confirmation": "revoke"},
        )
        assert invalid.status_code == 422
        assert profile.exists()
        export_path = f"{base}/{shop['id']}/login/export"
        export_payload = {"expected_revision": shop["revision"], "confirmation": "export"}
        export_method_denied = await request_as(
            client, "GET", export_path, "owner", PrincipalRole.OWNER,
        )
        assert export_method_denied.status_code == 405
        export_denied = await request_as(
            client, "POST", export_path, "member", PrincipalRole.MEMBER,
            json=export_payload,
        )
        assert export_denied.status_code == 403
        exported = await request_as(
            client, "POST", export_path, "owner", PrincipalRole.OWNER,
            json=export_payload,
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["cache-control"] == "no-store"
        assert exported.headers["content-type"].startswith("application/zip")
        assert exported.headers["x-content-type-options"] == "nosniff"
        with zipfile.ZipFile(io.BytesIO(exported.content)) as saved:
            assert saved.read("profile-1/Cookies") == b"private-cookie-secret"
        deleted = await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER, json=payload,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.headers["cache-control"] == "no-store"
        assert deleted.json()["browser_profile_present"] is False
        assert "private-cookie-secret" not in deleted.text
        assert not profile.exists()
        missing = await request_as(
            client, "POST", export_path, "owner", PrincipalRole.OWNER,
            json={**export_payload, "expected_revision": deleted.json()["revision"]},
        )
        assert missing.status_code == 409
