from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp_test_client import call_sdk_tool
from test_marketplace_api import FakeRuntime

from ophira_backend import browser_tools, web_tools
from ophira_backend.auth import AuthorizationError
from ophira_backend.config import Settings
from ophira_backend.constants import (
    BROWSER_MCP_SERVER_NAME,
    TOOL_ACT_ON_PAGE,
    TOOL_SNAPSHOT_PAGE,
    RunStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.maintenance import DataDirectoryBusyError, data_directory_lease
from ophira_backend.models import ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.shop_browser_host import ShopBrowserPage, serve
from ophira_backend.shop_browser_store import (
    ShopBrowserError,
    ShopBrowserOperation,
    ShopGrantInput,
    ShopIdentityInput,
)

PORTAL = "https://seller.example.com/"
HTML = b"""<!doctype html><title>Fixture seller portal</title><body>
<h1>Seller workspace</h1><div id="account"></div>
<input aria-label="Shop name" id="name"><button id="login"
onclick="localStorage.setItem('fixture-name',document.querySelector('#name').value);
location.reload()">Confirm shop</button>
<a href="/report.csv?signature=private-link">Export report</a>
<script>document.querySelector('#account').textContent=
localStorage.getItem('fixture-name')||'No shop selected'</script>
</body>"""
REPORT = b"product,quantity\nKettle,12\nCup,8\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _host: ["93.184.216.34"])
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    service = app.state.shop_browsers
    project = service.ledger.create_project(
        ProjectCreate(name="Shop operations", objective="Review listings")
    )
    run = service.ledger.create_run(project.id, "Read shop reports")
    run = service.ledger.update_run(run.id, status=RunStatus.RUNNING, sdk_session_id="sdk-shop-1")
    return app, service, project, run


async def create_shop(service, project, *, name="My shop"):
    return await service.create(
        project.id,
        ShopIdentityInput(
            name=name,
            seller_shop_id=name,
            platform="other",
            entry_url=PORTAL,
        ),
        None,
    )


def fixture_network(monkeypatch, *, html=HTML):
    async def fetch(request, **kwargs):
        from urllib.parse import urlsplit

        if urlsplit(request["url"]).hostname != "seller.example.com":
            return None, "outside fixture scope"
        report = "/report.csv" in request["url"]
        return {
            "responseCode": 200,
            "responseHeaders": [
                {"name": "Content-Type", "value": "text/csv" if report else "text/html"},
                *(
                    [{"name": "Content-Disposition", "value": 'attachment; filename="sales.csv"'}]
                    if report
                    else []
                ),
            ],
            "body": base64.b64encode(REPORT if report else html).decode(),
        }, None

    monkeypatch.setattr(browser_tools, "_fetch_pinned_browser_resource", fetch)


@asynccontextmanager
async def running(service, shop):
    if os.environ.get("OPHIRA_SHOP_BROWSER_E2E") != "1":
        pytest.skip("Set OPHIRA_SHOP_BROWSER_E2E=1 for offline real Chromium checks")
    task = asyncio.create_task(
        serve(
            service.ledger.root,
            shop["id"],
            headless=True,
            raise_errors=True,
            install_signals=False,
        )
    )
    try:
        async with asyncio.timeout(20):
            while True:
                session = service._live(shop["id"])
                if session and session["state"] == "open":
                    break
                if task.done():
                    await task
                    pytest.fail("fixture browser did not start")
                await asyncio.sleep(0.05)
        yield session
    finally:
        session = service._live(shop["id"])
        if session:
            await service._rpc(shop["id"], session, ShopBrowserOperation(operation="close"))
        await asyncio.wait_for(task, 10)


async def allow(service, project, shop, session):
    return await service.grant(
        project.id,
        shop["id"],
        ShopGrantInput(
            expected_revision=shop["revision"],
            session_id=session["id"],
            confirmed_identity=True,
        ),
        None,
    )


async def test_real_chromium_profile_can_be_exported_deleted_and_reopened(setup):
    _app, service, project, _run = setup
    shop = await create_shop(service, project)
    directory = service.store.directory(shop["id"])
    async with running(service, shop) as session:
        assert session["profile_generation"] == shop["profile_generation"]
    profile = directory / "profile-1"
    assert profile.is_dir()
    archive = service.export_login(project.id, shop["id"], shop["revision"], None)
    try:
        with zipfile.ZipFile(archive) as saved:
            assert any(
                name.startswith("profile-1/") and not name.endswith("/")
                for name in saved.namelist()
            )
            assert "session.json" not in saved.namelist()
    finally:
        archive.close()
    deleted = service.delete_login(project.id, shop["id"], shop["revision"], None)
    assert deleted["browser_login_data_present"] is False
    assert not profile.exists()
    async with running(service, deleted) as session:
        assert session["profile_generation"] == deleted["profile_generation"]
        assert (directory / "profile-2").is_dir()


async def test_real_browser_rpc_download_persists_exact_report_and_sources(setup, monkeypatch):
    app, service, project, run = setup
    fixture_network(monkeypatch)
    shop = await create_shop(service, project)
    async with running(service, shop) as session:
        with (
            pytest.raises(DataDirectoryBusyError),
            data_directory_lease(
                service.ledger.root,
                exclusive=True,
            ),
        ):
            pytest.fail("Active shop browser must prevent cold backup")
        await allow(service, project, shop, session)
        result = await service.capture(
            project.id, run.id, shop["id"], ShopBrowserOperation(operation="snapshot")
        )
        export = next(item for item in result["elements"] if item["label"] == "Export report")
        downloaded = await service.capture(
            project.id,
            run.id,
            shop["id"],
            ShopBrowserOperation(
                operation="download",
                observation_id=result["observation_id"],
                target=export["id"],
            ),
        )
        assert downloaded["shop_id"] == shop["id"]
        assert downloaded["identity_source"] == "human_confirmed"
        artifact = next(item for item in downloaded["artifacts"] if item["path"].endswith(".csv"))
        assert artifact["name"] == "sales.csv"
        assert artifact["checksum"] == hashlib.sha256(REPORT).hexdigest()
        assert (
            service.ledger.workspace_for_project(project.id) / artifact["path"]
        ).read_bytes() == REPORT
        evidence = next(
            item
            for item in service.ledger.get_project(project.id).evidence
            if item.id == downloaded["evidence_id"]
        )
        receipt = service.ledger.replay_tool_receipt(project.id, evidence.receipt_id)
        assert receipt.payload["shop_id"] == shop["id"]
        assert (
            next(item for item in receipt.payload["files"] if item["kind"] == "report")["sha256"]
            == artifact["checksum"]
        )
        assert "private-link" not in json.dumps(downloaded)
        assert session["token"] not in json.dumps(service.list(project.id))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            response = await client.get(artifact["download_url"])
            assert response.status_code == 200 and response.content == REPORT
            data = await client.get(f"/api/projects/{project.id}/shops/{shop['id']}/data")
            assert data.status_code == 200 and len(data.json()) == 2
    assert service.view(project.id, shop["id"])["browser_state"] == "closed"


async def test_real_profiles_keep_shop_login_separate_and_require_new_session_confirmation(
    setup, monkeypatch
):
    _app, service, project, _run = setup
    fixture_network(monkeypatch)
    first = await create_shop(service, project, name="Shop A")
    second = await create_shop(service, project, name="Shop B")
    async with running(service, first) as session:
        page = await service._rpc(first["id"], session, ShopBrowserOperation(operation="snapshot"))
        field = next(item for item in page["elements"] if item["label"] == "Shop name")
        page = await service._rpc(
            first["id"],
            session,
            ShopBrowserOperation(
                operation="type",
                observation_id=page["observation_id"],
                target=field["id"],
                value="Shop A",
            ),
        )
        button = next(item for item in page["elements"] if item["label"] == "Confirm shop")
        await service._rpc(
            first["id"],
            session,
            ShopBrowserOperation(
                operation="click",
                observation_id=page["observation_id"],
                target=button["id"],
            ),
        )
        await allow(service, project, first, session)
    async with running(service, first) as renewed:
        page = await service._rpc(first["id"], renewed, ShopBrowserOperation(operation="snapshot"))
        assert "Shop A" in page["excerpt"]
        assert not service.view(project.id, first["id"])["agent_access"]
        async with running(service, second) as other:
            isolated = await service._rpc(
                second["id"], other, ShopBrowserOperation(operation="snapshot")
            )
            assert "No shop selected" in isolated["excerpt"]
            assert "Shop A" not in isolated["excerpt"]


async def test_login_wall_prevents_grant_without_claiming_authenticated(setup, monkeypatch):
    _app, service, project, _run = setup
    fixture_network(monkeypatch, html=b'<title>Sign in</title><body><input type="password"></body>')
    shop = await create_shop(service, project)
    async with running(service, shop) as session:
        with pytest.raises(ShopBrowserError, match="sign-in state") as failure:
            await allow(service, project, shop, session)
        assert failure.value.code == "SHOP_LOGIN_REQUIRED"
        assert not service.view(project.id, shop["id"])["agent_access"]
        assert service.ledger.get_project(project.id).evidence == []


async def test_wrong_project_and_expired_run_cannot_use_shop_session(setup):
    _app, service, project, run = setup
    shop = await create_shop(service, project)
    other = service.ledger.create_project(ProjectCreate(name="Other", objective="Other"))
    with pytest.raises(AuthorizationError):
        service.view(other.id, shop["id"])
    service.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    with pytest.raises(AuthorizationError):
        await service.capture(
            project.id, run.id, shop["id"], ShopBrowserOperation(operation="snapshot")
        )


async def test_native_sdk_mcp_hook_preserves_shop_sources_and_requires_action_permission(
    setup,
    monkeypatch,
):
    _, service, project, run = setup
    fixture_network(
        monkeypatch,
        html=HTML.replace(
            b"</body>",
            (
                b"<p>"
                + b"x" * 12000
                + b"</p>"
                + b"".join(
                    b'<button aria-label="' + b"z" * 160 + b'">Action</button>' for _ in range(70)
                )
                + b"</body>"
            ),
        ),
    )
    shop = await create_shop(service, project)
    events = EventPublisher(service.ledger)
    runtime = ClaudeAgentRuntime(
        service.authority.settings,
        service.ledger,
        events,
        PermissionBroker(service.ledger, events, 1),
        shop_browsers=service,
        marketplace_access=service.authority,
    )
    options = runtime._build_options(run)
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.strict_mcp_config is True
    assert str(service.store.root.resolve()) in options.sandbox["filesystem"]["denyRead"]
    hook = runtime._hook(run.id, service.ledger.workspace_for_project(project.id))
    server = options.mcp_servers[BROWSER_MCP_SERVER_NAME]["instance"]
    request = {"shop_id": shop["id"]}
    async with running(service, shop) as session:
        await allow(service, project, shop, session)
        native = await call_sdk_tool(server, "snapshot_page", request)
        assert native.is_error is False
        raw = json.loads(native.content[0].text)
        assert len(raw["elements"]) == 60
        transformed = await hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": TOOL_SNAPSHOT_PAGE,
                "tool_input": request,
                "tool_response": {
                    "content": [block.model_dump(mode="json") for block in native.content]
                },
                "tool_use_id": "shop-capture",
            },
            "shop-capture",
            {},
        )
        visible = json.loads(
            transformed["hookSpecificOutput"]["updatedToolOutput"]["content"][0]["text"]
        )
        assert visible == raw
        evidence = service.ledger.get_project(project.id).evidence[0]
        receipt = service.ledger.replay_tool_receipt(project.id, evidence.receipt_id)
        for key in ("elements", "observation_id", "files", "shop_id", "excerpt", "source_url"):
            assert visible[key] == receipt.payload[key]
        action = {
            "shop_id": shop["id"],
            "action": "download",
            "selector": "element_2",
            "observation_id": raw["observation_id"],
        }
        permission = await hook(
            {"hook_event_name": "PreToolUse", "tool_name": TOOL_ACT_ON_PAGE, "tool_input": action},
            "shop-download",
            {},
        )
        assert permission["hookSpecificOutput"]["permissionDecision"] == "ask"
        for tool_name, arguments in ((TOOL_SNAPSHOT_PAGE, request), (TOOL_ACT_ON_PAGE, action)):
            branch = await options.can_use_tool(
                tool_name, arguments, SimpleNamespace(agent_id="research-branch")
            )
            assert branch.behavior == "deny"


async def test_real_stale_element_and_wrong_socket_secret_cannot_act(setup, monkeypatch):
    _, service, project, run = setup
    fixture_network(monkeypatch)
    drivers = []
    original = ShopBrowserPage.snapshot

    async def observe(driver):
        drivers.append(driver)
        return await original(driver)

    monkeypatch.setattr(ShopBrowserPage, "snapshot", observe)
    shop = await create_shop(service, project)
    async with running(service, shop) as session:
        await allow(service, project, shop, session)
        snapshot = await service.capture(
            project.id, run.id, shop["id"], ShopBrowserOperation(operation="snapshot")
        )
        element = next(item for item in snapshot["elements"] if item["label"] == "Export report")
        await drivers[-1].page.locator("a").evaluate("el => el.setAttribute('href', '/changed')")
        with pytest.raises(ShopBrowserError) as failure:
            await service.capture(
                project.id,
                run.id,
                shop["id"],
                ShopBrowserOperation(
                    operation="download",
                    observation_id=snapshot["observation_id"],
                    target=element["id"],
                ),
            )
        assert failure.value.code == "SHOP_TARGET_CHANGED"
        with pytest.raises(ShopBrowserError):
            await service._rpc(
                shop["id"],
                {**session, "token": "wrong"},
                ShopBrowserOperation(operation="snapshot"),
            )
        assert len(service.ledger.get_project(project.id).evidence) == 1
        assert list((service.store.directory(shop["id"]) / "staging").iterdir()) == []
