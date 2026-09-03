import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ophira_backend import browser_tools
from ophira_backend.browser_tools import (
    _install_cdp_request_guard,
    _install_l2_service_worker_guard,
    _install_request_guard,
    _playwright_capture,
    _remove_cdp_request_guard,
    _remove_l2_service_worker_guard,
    _screenshot_path,
    build_browser_tools,
    check_browser_target,
    domain_allowed,
)
from ophira_backend.config import Settings
from ophira_backend.connectors import (
    ConnectorRegistry,
    ConnectorUnavailableError,
    check_loopback_cdp_endpoint,
)
from ophira_backend.constants import (
    BROWSER_MCP_SERVER_NAME,
    TOOL_ACT_ON_PAGE,
    TOOL_SNAPSHOT_PAGE,
)
from ophira_backend.ledger_tools import build_mcp_servers
from ophira_backend.main import create_app
from ophira_backend.models import OrgPolicy, ProjectCreate
from ophira_backend.storage import FileLedger


def _tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9222",
        "https://localhost:9222",
        "ws://[::1]:9222/devtools/browser/id",
        "wss://127.0.0.2:9222/devtools/browser/id",
    ],
)
def test_cdp_endpoint_accepts_only_explicit_loopback(endpoint: str) -> None:
    assert check_loopback_cdp_endpoint(endpoint) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "http://localhost.evil:9222",
        "http://10.0.0.3:9222",
        "http://127.0.0.1",
        "ftp://127.0.0.1:9222",
        "http://user:secret@127.0.0.1:9222",
        "http://127.0.0.1:9222?token=secret",
    ],
)
def test_cdp_endpoint_rejects_remote_ambiguous_or_credentialed_hosts(endpoint: str) -> None:
    assert check_loopback_cdp_endpoint(endpoint)


def test_domain_allowlist_uses_hostname_boundaries() -> None:
    allowlist = ["example.com", "https://xn--bcher-kva.example/path"]
    assert domain_allowed("https://example.com/a", allowlist)
    assert domain_allowed("https://shop.example.com/a", allowlist)
    assert domain_allowed("https://bücher.example/a", allowlist)
    assert not domain_allowed("https://example.com.evil/a", allowlist)
    assert not domain_allowed("https://notexample.com/a", allowlist)


def test_browser_target_enforces_policy_and_private_network() -> None:
    assert "禁用" in (
        check_browser_target("https://example.com", OrgPolicy(browser_enabled=False)) or ""
    )
    assert "SSRF" in (
        check_browser_target("http://127.0.0.1/admin", OrgPolicy()) or ""
    )


def test_connector_registry_requires_enabled_healthy_loopback_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ConnectorRegistry(tmp_path / "connectors")
    registry.ensure_catalog()
    with pytest.raises(ConnectorUnavailableError, match="健康检查"):
        registry.require_healthy_browser_endpoint()

    monkeypatch.setattr("ophira_backend.connectors._cdp_endpoint_reachable", lambda _url: True)
    checked = registry.health_check("chrome_cdp")
    assert checked.health == "ok"
    assert registry.require_healthy_browser_endpoint() == "http://127.0.0.1:9222"

    revoked = registry.revoke("chrome_cdp")
    assert revoked.enabled is False
    with pytest.raises(ConnectorUnavailableError, match="停用"):
        registry.require_healthy_browser_endpoint()


def test_connector_registry_rejects_non_loopback_cdp_configuration(tmp_path: Path) -> None:
    registry = ConnectorRegistry(tmp_path / "connectors")
    with pytest.raises(ValueError, match="loopback"):
        registry.upsert(
            "chrome_cdp",
            type="chrome_cdp",
            display_name="unsafe",
            endpoint="http://169.254.169.254:9222",
        )


def test_browser_tool_schema_never_accepts_a_model_supplied_endpoint(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    tools = build_browser_tools(ledger, project.id, Settings(data_dir=tmp_path))
    for item in tools:
        properties = item.input_schema["properties"]
        assert "cdp_endpoint" not in properties
        assert "use_connected_browser" in properties
        assert item.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_l2_endpoint_comes_from_connector_and_capture_is_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Open supplier page")
    seen: dict[str, Any] = {}

    class FakeConnectors:
        def require_healthy_browser_endpoint(self) -> str:
            return "http://127.0.0.1:9333"

    async def fake_capture(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        screenshot = kwargs["workspace"] / "evidence-screens" / "capture.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"png")
        return {
            "ok": True,
            "url": kwargs["url"],
            "final_url": kwargs["url"],
            "title": "Supplier",
            "excerpt": "Observed supplier page",
            "action": kwargs["action"],
            "screenshot_path": "evidence-screens/capture.png",
            "browser_level": "L2",
        }

    monkeypatch.setattr(browser_tools, "_playwright_capture", fake_capture)
    tools = build_browser_tools(
        ledger,
        project.id,
        Settings(data_dir=tmp_path),
        connectors=FakeConnectors(),  # type: ignore[arg-type]
        run_id=run.id,
    )
    response = await tools[0].handler(
        {"url": "https://supplier.example", "use_connected_browser": True}
    )
    payload = _tool_payload(response)

    assert response["is_error"] is False
    assert seen["cdp_endpoint"] == "http://127.0.0.1:9333"
    assert payload["artifact_id"].startswith("artifact_")
    assert payload["evidence_id"].startswith("ev_")
    saved = ledger.get_project(project.id)
    artifact = saved.artifacts[0]
    evidence = saved.evidence[0]
    assert artifact.run_id == run.id
    assert artifact.source_ids == [evidence.id]
    assert evidence.source_run_id == run.id
    assert evidence.source_path == artifact.path


@pytest.mark.asyncio
async def test_l1_uses_dedicated_persistent_profile_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    seen: dict[str, Any] = {}

    async def fake_capture(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": False, "error_code": "EXPECTED", "error": "stop"}

    monkeypatch.setattr(browser_tools, "_playwright_capture", fake_capture)
    tools = build_browser_tools(ledger, project.id, Settings(data_dir=tmp_path))
    await tools[0].handler({"url": "https://example.com"})

    assert seen["cdp_endpoint"] is None
    assert seen["profile_dir"] == tmp_path / "browser-profiles" / project.id / "l1"
    assert not seen["profile_dir"].is_relative_to(ledger.workspace_for_project(project.id))


def test_browser_server_is_not_registered_when_org_policy_disables_it(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    servers = build_mcp_servers(
        ledger,
        project.id,
        settings=Settings(data_dir=tmp_path),
        policy=OrgPolicy(browser_enabled=False),
    )
    assert BROWSER_MCP_SERVER_NAME not in servers


def test_runtime_uses_shared_connector_registry_and_removes_disabled_browser_tool(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path))
    project = app.state.ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate")
    )
    app.state.policy.save(OrgPolicy(browser_enabled=False))
    run = app.state.ledger.create_run(project.id, "Check supplier")
    options = app.state.runtime._build_options(run)

    assert app.state.runtime.connectors is app.state.connectors
    assert app.state.runtime.policy is app.state.policy
    assert BROWSER_MCP_SERVER_NAME not in (options.mcp_servers or {})
    assert TOOL_SNAPSHOT_PAGE not in (options.allowed_tools or [])


@pytest.mark.asyncio
async def test_browser_actions_are_forced_to_ask_even_in_auto_policy(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path))
    project = app.state.ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate")
    )
    run = app.state.ledger.create_run(project.id, "Submit supplier form")
    app.state.policy.save(OrgPolicy(permission_mode="auto"))
    hook = app.state.runtime._hook(
        run.id,
        app.state.ledger.workspace_for_project(project.id),
    )

    result = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": TOOL_ACT_ON_PAGE,
            "tool_input": {
                "url": "https://supplier.example",
                "action": "click",
                "selector": "button",
            },
        },
        None,
        None,
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.asyncio
async def test_request_guard_blocks_new_private_request_but_ignores_existing_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_page = object()
    new_page = object()

    async def fake_check(url: str, _policy: OrgPolicy) -> str | None:
        return "private" if "127.0.0.1" in url else None

    monkeypatch.setattr(browser_tools, "_check_browser_target", fake_check)

    class Context:
        async def route(self, _pattern: str, handler: Any) -> None:
            self.handler = handler

    class Frame:
        def __init__(self, page: object) -> None:
            self.page = page

    class Request:
        def __init__(self, url: str, page: object) -> None:
            self.url = url
            self.frame = Frame(page)

    class Route:
        def __init__(self, url: str, page: object) -> None:
            self.request = Request(url, page)
            self.result = ""

        async def fallback(self) -> None:
            self.result = "continued"

        async def abort(self, _reason: str) -> None:
            self.result = "aborted"

    context = Context()
    blocked: list[dict[str, str]] = []
    guard = await _install_request_guard(
        context,
        policy=OrgPolicy(),
        baseline_pages={existing_page},
        blocked=blocked,
    )
    existing = Route("http://127.0.0.1/private", existing_page)
    created = Route("http://127.0.0.1/private", new_page)
    await guard(existing)
    await guard(created)

    assert existing.result == "continued"
    assert created.result == "aborted"
    assert blocked == [{"url": "http://127.0.0.1/private", "reason": "private"}]


@pytest.mark.asyncio
async def test_request_guard_fails_closed_for_unowned_popup_navigation() -> None:
    class Context:
        async def route(self, _pattern: str, handler: Any) -> None:
            self.handler = handler

    class Request:
        url = "https://public.example/redirector"

        @property
        def frame(self) -> object:
            raise RuntimeError("popup frame does not exist yet")

        def is_navigation_request(self) -> bool:
            return True

    class Route:
        request = Request()
        result = ""

        async def abort(self, _reason: str) -> None:
            self.result = "aborted"

    context = Context()
    blocked: list[dict[str, str]] = []
    guard = await _install_request_guard(
        context,
        policy=OrgPolicy(),
        baseline_pages=set(),
        blocked=blocked,
    )
    route = Route()
    await guard(route)

    assert route.result == "aborted"
    assert blocked[0]["url"] == "https://public.example/redirector"


@pytest.mark.asyncio
async def test_cdp_guard_blocks_a_private_server_redirect_before_request_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_check(url: str, _policy: OrgPolicy) -> str | None:
        return "private" if "127.0.0.1" in url else None

    monkeypatch.setattr(browser_tools, "_check_browser_target", fake_check)

    class Session:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}
            self.sent: list[tuple[str, dict[str, Any] | None]] = []
            self.detached = False

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

        def remove_listener(self, event: str, handler: Any) -> None:
            assert self.handlers[event] is handler
            del self.handlers[event]

        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
        ) -> None:
            self.sent.append((method, params))

        async def detach(self) -> None:
            self.detached = True

    session = Session()

    class Context:
        async def new_cdp_session(self, _page: object) -> Session:
            return session

    blocked: list[dict[str, str]] = []
    installed, pending, handler = await _install_cdp_request_guard(
        Context(),
        object(),
        policy=OrgPolicy(),
        blocked=blocked,
    )
    session.handlers["Fetch.requestPaused"](
        {
            "requestId": "redirect-hop-2",
            "redirectedRequestId": "redirect-hop-1",
            "request": {"url": "http://127.0.0.1/admin"},
        }
    )
    await asyncio.sleep(0)
    await _remove_cdp_request_guard(installed, pending, handler)

    assert (
        "Fetch.failRequest",
        {"requestId": "redirect-hop-2", "errorReason": "BlockedByClient"},
    ) in session.sent
    assert (
        "Network.setBypassServiceWorker",
        {"bypass": True},
    ) in session.sent
    assert (
        "Network.setBypassServiceWorker",
        {"bypass": False},
    ) in session.sent
    assert not any(method == "Fetch.continueRequest" for method, _ in session.sent)
    assert blocked == [{"url": "http://127.0.0.1/admin", "reason": "private"}]
    assert session.detached is True


@pytest.mark.asyncio
async def test_new_l2_service_worker_closes_only_the_ophira_page() -> None:
    class Page:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class Context:
        service_workers: list[object] = []

        def on(self, event: str, handler: Any) -> None:
            self.handlers = {event: handler}

        def remove_listener(self, event: str, handler: Any) -> None:
            assert self.handlers[event] is handler
            del self.handlers[event]

    class Worker:
        url = "https://public.example/sw.js"

    context = Context()
    ophira_page = Page()
    user_page = Page()
    handler, detected, close_tasks = _install_l2_service_worker_guard(
        context,
        {ophira_page},
    )
    context.handlers["serviceworker"](Worker())
    await asyncio.sleep(0)
    await _remove_l2_service_worker_guard(context, handler, close_tasks)

    assert detected == ["https://public.example/sw.js"]
    assert ophira_page.closed is True
    assert user_page.closed is False
    assert context.handlers == {}


def test_screenshot_names_do_not_collide(tmp_path: Path) -> None:
    assert _screenshot_path(tmp_path) != _screenshot_path(tmp_path)


def test_screenshot_directory_cannot_escape_workspace_via_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "evidence-screens").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="workspace|symlink"):
        _screenshot_path(workspace)


@pytest.mark.asyncio
async def test_l2_cleanup_closes_only_pages_created_by_ophira(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

        def on(self, _event: str, handler: Any) -> None:
            self.popup_handler = handler

        async def add_init_script(self, _script: str) -> None:
            return None

        async def goto(self, url: str, **_kwargs: Any) -> None:
            self.url = url

        async def title(self) -> str:
            return "Supplier"

        async def inner_text(self, _selector: str) -> str:
            return "Supplier page"

        async def screenshot(self, *, path: str, **_kwargs: Any) -> None:
            await asyncio.to_thread(Path(path).write_bytes, b"png")

    existing_page = Page("https://mail.example")

    class Context:
        def __init__(self) -> None:
            self.pages = [existing_page]
            self.closed = False
            self.service_workers: list[object] = []
            self.event_handlers: dict[str, Any] = {}

        def on(self, event: str, handler: Any) -> None:
            self.event_handlers[event] = handler

        def remove_listener(self, event: str, handler: Any) -> None:
            assert self.event_handlers[event] is handler
            del self.event_handlers[event]

        async def route(self, _pattern: str, handler: Any) -> None:
            self.handler = handler

        async def unroute(self, _pattern: str, handler: Any) -> None:
            assert handler is self.handler

        async def new_page(self) -> Page:
            page = Page("about:blank")
            self.pages.append(page)
            return page

        async def close(self) -> None:
            self.closed = True

    context = Context()

    class Session:
        def on(self, _event: str, handler: Any) -> None:
            self.handler = handler

        def remove_listener(self, _event: str, handler: Any) -> None:
            assert handler is self.handler

        async def send(self, _method: str, _params: Any = None) -> None:
            return None

        async def detach(self) -> None:
            return None

    async def new_cdp_session(_page: Page) -> Session:
        return Session()

    context.new_cdp_session = new_cdp_session  # type: ignore[attr-defined]

    class Browser:
        contexts = [context]

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    browser = Browser()

    class Chromium:
        async def connect_over_cdp(self, _endpoint: str, **_kwargs: Any) -> Browser:
            return browser

    class Playwright:
        chromium = Chromium()

    class PlaywrightManager:
        async def __aenter__(self) -> Playwright:
            return Playwright()

        async def __aexit__(self, *_args: Any) -> None:
            return None

    async def allow_target(_url: str, _policy: OrgPolicy) -> str | None:
        return None

    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = PlaywrightManager
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(browser_tools, "_check_browser_target", allow_target)

    result = await _playwright_capture(
        url="https://supplier.example",
        workspace=tmp_path / "workspace",
        profile_dir=tmp_path / "profile",
        policy=OrgPolicy(),
        timeout_seconds=2,
        cdp_endpoint="http://127.0.0.1:9222",
    )

    assert result["ok"] is True
    assert browser.close_calls == 0
    assert context.closed is False
    assert existing_page.closed is False
    created_pages = [item for item in context.pages if item is not existing_page]
    assert created_pages and all(item.closed for item in created_pages)

    class Worker:
        url = "https://mail.example/sw.js"

    context.service_workers = [Worker()]
    unsafe = await _playwright_capture(
        url="https://supplier.example",
        workspace=tmp_path / "workspace",
        profile_dir=tmp_path / "profile",
        policy=OrgPolicy(),
        timeout_seconds=2,
        cdp_endpoint="http://127.0.0.1:9222",
    )

    assert unsafe["error_code"] == "BROWSER_SERVICE_WORKER_UNSAFE"
    assert unsafe["active_service_worker_count"] == 1
    assert browser.close_calls == 0
    assert context.closed is False
    assert existing_page.closed is False
    assert context.event_handlers == {}
