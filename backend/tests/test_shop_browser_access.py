from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_auth_api import principal, request_as, trusted_settings
from test_marketplace_api import FakeRuntime
from test_shop_browser import create_shop
from test_shop_browser import setup as shop_setup

from ophira_backend import web_tools
from ophira_backend.auth import AuthorizationError
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import new_id, utc_now
from ophira_backend.shop_browser_host import (
    ShopBrowserPolicy,
    authorize_caller,
    execute_authorized,
)
from ophira_backend.shop_browser_store import (
    ShopBrowserCaller,
    ShopBrowserError,
    ShopBrowserOperation,
    ShopIdentityInput,
    ShopUpdateInput,
)

setup = shop_setup


@pytest.mark.parametrize("tamper", ["hash", "size", "symlink", "path"])
async def test_unverified_staged_file_cannot_create_project_evidence(setup, tamper):
    _, service, project, run = setup
    view = await create_shop(service, project)
    shop = service.store.get(view["id"])
    stage = service.store.directory(shop.id) / "staging"
    stage.mkdir()
    name = f"{new_id('page')}.png"
    content = b"synthetic screenshot bytes"
    path = stage / name
    path.write_bytes(content)
    metadata = {
        "name": name,
        "kind": "screenshot",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if tamper == "hash":
        metadata["sha256"] = "0" * 64
    elif tamper == "size":
        metadata["size"] += 1
    elif tamper == "path":
        metadata["name"] = "../session.json"
    else:
        path.unlink()
        outside = stage.parent / "private"
        outside.write_bytes(content)
        path.symlink_to(outside)
    with pytest.raises(ShopBrowserError):
        service._record(
            project.id,
            run.id,
            shop,
            {"files": [metadata]},
            ShopBrowserOperation(operation="snapshot"),
            lambda: None,
        )
    current = service.ledger.get_project(project.id)
    assert not current.evidence and not current.artifacts
    assert service.ledger.list_tool_receipts(project.id) == []


def bind(service, shop, run):
    session_id = new_id("shop_session")
    current = service.store.get(shop["id"])
    now = utc_now()
    service.store.write(
        current.model_copy(
            update={
                "authorized_session_id": session_id,
                "granted_at": now,
                "grant_expires_at": now + timedelta(hours=1),
            }
        )
    )
    return session_id, ShopBrowserCaller(
        project_id=run.project_id,
        run_id=run.id,
        sdk_session_id=run.sdk_session_id,
        shop_revision=current.revision,
    )


@pytest.mark.parametrize("change", ["revoke", "run", "sdk", "shop", "expiry"])
async def test_worker_rechecks_durable_authority_before_executing_queued_command(setup, change):
    _, service, project, run = setup
    shop = await create_shop(service, project)
    session, caller = bind(service, shop, run)
    operation = ShopBrowserOperation(operation="snapshot")

    def check():
        authorize_caller(
            service.store, service.authority.access, shop["id"], session, caller, operation
        )

    check()
    if change == "revoke":
        service.revoke(project.id, shop["id"], shop["revision"], None)
    elif change == "run":
        service.ledger.update_run(run.id, status=RunStatus.CANCELLED)
    elif change == "sdk":
        service.ledger.update_run(run.id, sdk_session_id="another-session")
    else:
        current = service.store.get(shop["id"])
        updates = (
            {"revision": current.revision + 1}
            if change == "shop"
            else {
                "granted_at": utc_now() - timedelta(hours=2),
                "grant_expires_at": utc_now() - timedelta(hours=1),
            }
        )
        service.store.write(current.model_copy(update=updates))
    with pytest.raises(AuthorizationError):
        check()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_inflight_operation_is_cancelled_and_staged_capture_discarded(setup, disconnect):
    _, service, project, run = setup
    shop = await create_shop(service, project)
    session, caller = bind(service, shop, run)
    operation = ShopBrowserOperation(operation="snapshot")
    started, cancelled = asyncio.Event(), asyncio.Event()
    discarded = []

    async def execute(_operation):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    driver = SimpleNamespace(
        execute=execute,
        policy=ShopBrowserPolicy(service.store.get(shop["id"]), service.policy),
        discard_capture=lambda: discarded.append(True),
    )
    reader = asyncio.StreamReader()
    task = asyncio.create_task(
        execute_authorized(
            driver,
            operation,
            lambda: authorize_caller(
                service.store,
                service.authority.access,
                shop["id"],
                session,
                caller,
                operation,
            ),
            reader,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    if disconnect:
        reader.feed_eof()
    else:
        service.revoke(project.id, shop["id"], shop["revision"], None)
        assert not driver.policy.browser_enabled
    with pytest.raises(AuthorizationError):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set() and discarded == [True]
    assert driver.policy.authorize is None
    assert service.ledger.get_project(project.id).evidence == []


async def test_edit_identity_uses_new_profile_and_stale_revision_cannot_overwrite(setup):
    _, service, project, _ = setup
    shop = await create_shop(service, project)
    original = service.store.get(shop["id"])
    fields = {key: shop[key] for key in ShopIdentityInput.model_fields}
    payload = ShopUpdateInput(
        **{**fields, "seller_shop_id": "Another seller"}, expected_revision=shop["revision"]
    )
    saved = await service.update(project.id, shop["id"], payload, None)
    assert saved["revision"] == original.revision + 1
    assert saved["profile_generation"] == original.profile_generation + 1
    assert not saved["agent_access"]
    with pytest.raises(ShopBrowserError) as failure:
        await service.update(project.id, shop["id"], payload, None)
    assert failure.value.code == "SHOP_CONFLICT"


async def test_gateway_shop_routes_require_current_project_admin_and_hide_invalid_input(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://gateway.test"
    ) as client:
        created = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner",
            PrincipalRole.OWNER,
            json={"name": "Shop", "objective": "Read report"},
        )
        assert created.status_code in {200, 201}, created.text
        project = created.json()
        service = app.state.shop_browsers
        service.authority.access.grant(
            project["id"],
            principal("owner", PrincipalRole.OWNER),
            principal_id="member",
            role=PrincipalRole.MEMBER,
        )
        path = f"/api/projects/{project['id']}/shops"
        body = {
            "name": "My shop",
            "seller_shop_id": "seller-1",
            "platform": "other",
            "entry_url": "https://seller.example.com/",
        }
        denied = await request_as(client, "POST", path, "member", PrincipalRole.MEMBER, json=body)
        assert denied.status_code == 403
        saved = await request_as(client, "POST", path, "owner", PrincipalRole.OWNER, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        shop = saved.json()
        assert "token" not in shop and "profile_path" not in shop
        listed = await request_as(client, "GET", path, "member", PrincipalRole.MEMBER)
        assert listed.status_code == 200 and len(listed.json()) == 1
        outsider = await request_as(client, "GET", path, "outsider", PrincipalRole.OWNER)
        assert outsider.status_code == 403
        invalid = await request_as(
            client,
            "POST",
            path,
            "owner",
            PrincipalRole.OWNER,
            json={**body, "entry_url": "https://seller.example.com/?token=private-secret"},
        )
        assert invalid.status_code == 422 and "private-secret" not in invalid.text
        # Removing membership is checked in the worker too, using the durable bound Run.
        run = service.ledger.create_run(
            project["id"], "Read", principal=principal("member", PrincipalRole.MEMBER)
        )
        run = service.ledger.update_run(run.id, status=RunStatus.RUNNING, sdk_session_id="sdk")
        session, caller = bind(service, shop, run)
        operation = ShopBrowserOperation(operation="snapshot")
        authorize_caller(
            service.store, service.authority.access, shop["id"], session, caller, operation
        )
        service.authority.access.revoke(
            project["id"], principal("owner", PrincipalRole.OWNER), principal_id="member"
        )
        with pytest.raises(AuthorizationError):
            authorize_caller(
                service.store, service.authority.access, shop["id"], session, caller, operation
            )
