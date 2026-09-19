"""Deletion excludes late identity, marketplace, commercial and browser writers."""

import asyncio
import multiprocessing
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_commercial_adapter import (
    make_service,
    stage_and_approve,
    success_response,
    target_response,
)

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.config import Settings
from ophira_backend.connectors import ConnectorRegistry
from ophira_backend.constants import PrincipalRole
from ophira_backend.marketplace_access import MarketplaceAccessService, MarketplaceGrantRequest
from ophira_backend.models import PrincipalContext, ProjectCreate, new_id, utc_now
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.project_lifecycle import (
    ProjectLifecycle,
    ProjectLifecycleBusyError,
    ProjectUnavailableError,
)
from ophira_backend.providers import FileSecretVault
from ophira_backend.shop_browser import ShopBrowserService
from ophira_backend.shop_browser_host import ShopBrowserPage, launch_lease, serve, session_lease
from ophira_backend.shop_browser_store import (
    ShopBrowserOperation,
    ShopBrowserStore,
    ShopIdentity,
    ShopIdentityInput,
)
from ophira_backend.storage import FileLedger


def delete_intent(lifecycle, project_id):
    with lifecycle.lease(project_id, exclusive=True):
        lifecycle.write_state(project_id, {
            "schema_version": 1, "project_id": project_id, "operation_id": "operation_fixture",
            "status": "deleting", "plan": {},
        })


def owner():
    now = utc_now()
    return PrincipalContext(
        tenant_id="tenant_fixture", principal_id="owner_fixture", role=PrincipalRole.OWNER,
        session_id="session_fixture_1234567890", assertion_issued_at=now,
        assertion_nonce="nonce_fixture_1234567890",
        assertion_expires_at=now + timedelta(minutes=5),
    )


def hold_access_transaction(root, project_id, ready, finish):
    store = ProjectAccessStore(root / "auth" / "projects")
    with store._transaction(project_id):
        ready.set()
        assert finish.wait(15)


def test_auth_transaction_participates_across_processes_and_cannot_recreate_deleted_acl(tmp_path):
    project_id = "project_fixture"
    lifecycle = ProjectLifecycle(tmp_path)
    store = ProjectAccessStore(tmp_path / "auth" / "projects")
    principal = owner()
    store.bind_project(project_id, principal)
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(
        target=hold_access_transaction, args=(tmp_path, project_id, ready, finish),
    )
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ProjectLifecycleBusyError), lifecycle.lease(project_id, exclusive=True):
            pass
    finally:
        finish.set()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join(5)
    assert child.exitcode == 0
    delete_intent(lifecycle, project_id)
    shutil.rmtree(store.root / project_id)
    with pytest.raises(ProjectUnavailableError):
        store.bind_project(project_id, principal)
    with pytest.raises(ProjectUnavailableError):
        store.get(project_id)
    with pytest.raises(ProjectUnavailableError):
        store.grant(project_id, principal, principal_id="member", role=PrincipalRole.MEMBER)
    assert not (store.root / project_id).exists()


def marketplace(root, reader=None):
    ledger = FileLedger(root)
    return MarketplaceAccessService(
        ledger, ConnectorRegistry(root / "connectors", FileSecretVault(root / "secrets")),
        settings=Settings(_env_file=None, data_dir=root),
        access=ProjectAccessStore(root / "auth" / "projects"), reader=reader,
    )


async def test_marketplace_local_grant_holds_lease_across_read_and_rejects_late_write(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        started.set()
        await finish.wait()
        return {"shops": [{"id": "shop_fixture", "name": "Fixture", "region": "US"}]}

    reader = SimpleNamespace(
        read=read, require_grantable=lambda _: None, identity_parts=lambda _: ["fixture"],
    )
    service = marketplace(tmp_path, reader)
    project = service.ledger.create_project(ProjectCreate(name="Fixture", objective="Review"))
    payload = MarketplaceGrantRequest(
        connector_id="tiktok_shop", shop_id="shop_fixture", expected_revision=0,
    )
    task = asyncio.create_task(service.grant(project.id, payload, None))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(ProjectLifecycleBusyError), service.ledger.lifecycle.lease(
            project.id, exclusive=True,
        ):
            pass
    finally:
        finish.set()
    granted = await task
    delete_intent(service.ledger.lifecycle, project.id)
    path = service.root / f"{project.id}.json"
    path.unlink()
    with pytest.raises(ProjectUnavailableError):
        service.revoke(project.id, granted["bindings"][0]["id"], granted["revision"], None)
    with pytest.raises(ProjectUnavailableError), service._identity_transaction(project.id):
        pass
    assert not path.exists()


@pytest.mark.parametrize("operation", ["apply", "observe_target"])
def test_commercial_operation_excludes_deletion_through_receipt_commit(tmp_path, operation):
    started, finish = threading.Event(), threading.Event()
    requests = []

    def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            started.set()
            assert finish.wait(10)
            return target_response(request)
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    action_id, apply_request = stage_and_approve(service, project_id, run_id, session_id)

    def execute():
        if operation == "apply":
            return service.apply(
                project_id, action_id, apply_request, actor_id="owner@example.test",
            )
        return service.observe_target(
            project_id, source_run_id=run_id, source_session_id=session_id,
            connector_id="supplier_outreach", resource_type="supplier_inquiry",
            resource_key="inquiry-100",
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(execute)
            assert started.wait(5)
            try:
                with pytest.raises(ProjectLifecycleBusyError), service.ledger.lifecycle.lease(
                    project_id, exclusive=True,
                ):
                    pass
            finally:
                finish.set()
            pending.result(timeout=10)
        delete_intent(service.ledger.lifecycle, project_id)
        shutil.rmtree(service.store.root / project_id)
        before = list(requests)
        with pytest.raises(ProjectUnavailableError):
            execute()
        with pytest.raises(ProjectUnavailableError), service.store.transaction(project_id):
            pass
        assert requests == before and not (service.store.root / project_id).exists()
    finally:
        finish.set()
        client.close()


def stored_shop(root):
    authority = marketplace(root)
    project = authority.ledger.create_project(ProjectCreate(name="Shop", objective="Review"))
    service = ShopBrowserService(authority, OrgPolicyStore(root / "org" / "policy.json"))
    shop = ShopIdentity(
        project_id=project.id, name="Fixture", seller_shop_id="fixture", platform="other",
        entry_url="https://seller.example.com", created_by="local_owner",
        tenant_id=None, identity_binding="local_only",
    )
    service.store.write(shop)
    return service, project, shop


@pytest.mark.parametrize("lease", [session_lease, launch_lease])
def test_shop_lifetime_leases_exclude_deletion_and_store_blocks_stale_recreation(tmp_path, lease):
    service, project, shop = stored_shop(tmp_path)
    entered, finish = threading.Event(), threading.Event()

    def hold():
        with lease(service.store, shop.id):
            entered.set()
            assert finish.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(hold)
        assert entered.wait(5)
        try:
            with pytest.raises(ProjectLifecycleBusyError), service.ledger.lifecycle.lease(
                project.id, exclusive=True,
            ):
                pass
        finally:
            finish.set()
        task.result(timeout=10)
    delete_intent(service.ledger.lifecycle, project.id)
    shutil.rmtree(service.store.root / shop.id)
    for action in [lambda: service.store.directory(shop.id), lambda: service.store.session(shop.id),
                   lambda: service.store.write(shop), lambda: service.store.socket_path(shop.id)]:
        with pytest.raises(ProjectUnavailableError):
            action()
    assert not (service.store.root / shop.id).exists()


async def test_shop_create_keeps_lease_across_dns_validation(tmp_path, monkeypatch):
    service, project, _ = stored_shop(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def check(*args):
        started.set()
        await finish.wait()
        return None

    monkeypatch.setattr("ophira_backend.shop_browser._check_browser_target", check)
    task = asyncio.create_task(service.create(project.id, ShopIdentityInput(
        name="Second", seller_shop_id="second", platform="other",
        entry_url="https://seller.example.com",
    ), None))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(ProjectLifecycleBusyError), service.ledger.lifecycle.lease(
            project.id, exclusive=True,
        ):
            pass
    finally:
        finish.set()
    result = await task
    assert result["project_id"] == project.id


async def test_host_startup_does_not_create_project_directories_before_lease(tmp_path, monkeypatch):
    service, project, shop = stored_shop(tmp_path)
    assert not (service.store.root / shop.id).exists()

    def stale_get(self, shop_id):
        delete_intent(service.ledger.lifecycle, project.id)
        return shop

    def forbidden_launch():
        pytest.fail("deleted project must never launch a browser")

    monkeypatch.setattr(ShopBrowserStore, "get", stale_get)
    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(
        async_playwright=forbidden_launch,
    ))
    with pytest.raises(ProjectUnavailableError):
        await serve(tmp_path, shop.id, install_signals=False)
    assert not (service.store.root / shop.id).exists()


async def test_browser_driver_child_task_takes_its_own_lease(tmp_path, monkeypatch):
    service, project, shop = stored_shop(tmp_path)
    driver = ShopBrowserPage(service.store, shop.id, new_id("shop_session"), service.policy.get())
    started, finish = asyncio.Event(), asyncio.Event()

    async def execute(operation):
        started.set()
        await finish.wait()
        return {"ok": True}

    monkeypatch.setattr(driver, "_execute", execute)
    task = asyncio.create_task(driver.execute(ShopBrowserOperation(operation="snapshot")))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(ProjectLifecycleBusyError), service.ledger.lifecycle.lease(
            project.id, exclusive=True,
        ):
            pass
    finally:
        finish.set()
    assert await task == {"ok": True}
    with service.ledger.lifecycle.lease(project.id, exclusive=True):
        with pytest.raises(ProjectLifecycleBusyError):
            await asyncio.create_task(driver.execute(ShopBrowserOperation(operation="snapshot")))


def test_deleted_shops_do_not_break_another_projects_list(tmp_path):
    service, deleted, _ = stored_shop(tmp_path)
    other = service.ledger.create_project(ProjectCreate(name="Other", objective="Keep"))
    delete_intent(service.ledger.lifecycle, deleted.id)
    assert service.store.list(other.id) == []


async def test_another_project_exclusive_operation_does_not_block_shop_list(tmp_path):
    service, busy, _ = stored_shop(tmp_path)
    other = service.ledger.create_project(ProjectCreate(name="Other", objective="Keep"))
    with service.ledger.lifecycle.lease(busy.id, exclusive=True):
        assert await asyncio.to_thread(service.store.list, other.id) == []
