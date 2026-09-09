"""Policy-enforced Playwright GUI ladder (FR-20 / FR-22).

L1 launches an OPHIRA-owned persistent Chromium profile. L2 may attach only to
the enabled, healthy, loopback-only ``chrome_cdp`` connector; a model never
supplies a CDP endpoint. Every capture becomes both a registered Artifact and a
sourced Evidence item immediately.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .connectors import ConnectorRegistry, ConnectorUnavailableError
from .constants import BROWSER_MCP_SERVER_NAME, EvidenceKind, EvidenceSourceType
from .models import Evidence, OrgPolicy
from .storage import FileLedger, SourceReceiptScopeError
from .untrusted import (
    UNTRUSTED_DATA_NOTICE,
    UntrustedSource,
    sanitize_untrusted_payload,
)
from .web_tools import check_public_http_url, send_pinned_public_request

if TYPE_CHECKING:
    from .project_events import ProjectEventProjector
    from .shop_browser import ShopBrowserService

_BROWSER_RESPONSE_MAX_BYTES = 20_000_000
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_BLOCK_SERVICE_WORKER_REGISTRATION = """
(() => {
  const prototype = globalThis.ServiceWorkerContainer?.prototype;
  if (!prototype) return;
  Object.defineProperty(prototype, "register", {
    configurable: false,
    writable: false,
    value: () => Promise.reject(
      new DOMException("Service workers are disabled for this OPHIRA page", "SecurityError")
    ),
  });

  const denyNetworkConstructor = (name) => {
    if (!(name in globalThis)) return;
    Object.defineProperty(globalThis, name, {
      configurable: false,
      writable: false,
      value: class {
        constructor() {
          throw new DOMException(
            `${name} is disabled for this OPHIRA page`,
            "SecurityError",
          );
        }
      },
    });
  };
  for (const name of [
    "WebSocket",
    "WebTransport",
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
  ]) denyNetworkConstructor(name);
})();
"""


def _result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "is_error": is_error,
    }


def _error(error_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": message, **details}


def _screenshot_path(workspace: Path) -> Path:
    workspace = workspace.resolve()
    folder = workspace / "evidence-screens"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.resolve().relative_to(workspace)
    except ValueError as exc:
        raise ValueError("browser evidence directory escapes project workspace") from exc
    if folder.is_symlink():
        raise ValueError("browser evidence directory cannot be a symlink")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return folder / f"browser-{stamp}-{uuid4().hex[:10]}.png"


def _normalized_hostname(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if "://" in candidate:
        try:
            candidate = urlsplit(candidate).hostname or ""
        except ValueError:
            return None
    else:
        candidate = candidate.removeprefix("*.").lstrip(".")
        try:
            candidate = urlsplit(f"//{candidate}").hostname or ""
        except ValueError:
            return None
    try:
        return candidate.rstrip(".").encode("idna").decode("ascii").casefold() or None
    except UnicodeError:
        return None


def domain_allowed(url: str, allowlist: list[str]) -> bool:
    """Match an exact hostname or a real subdomain boundary."""
    if not allowlist:
        return True
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    normalized = _normalized_hostname(hostname)
    if not normalized:
        return False
    for item in allowlist:
        allowed = _normalized_hostname(item)
        if allowed and (normalized == allowed or normalized.endswith(f".{allowed}")):
            return True
    return False


def check_browser_target(url: str, policy: OrgPolicy) -> str | None:
    if not policy.browser_enabled:
        return "组织策略已禁用浏览器能力"
    reason = check_public_http_url(url)
    if reason:
        return reason
    if not domain_allowed(url, policy.domain_allowlist):
        return "目标域名不在组织 browser domain allowlist 中"
    return None


async def _check_browser_target(url: str, policy: OrgPolicy) -> str | None:
    # DNS resolution in the shared SSRF guard is synchronous. Route handlers
    # must not block the event loop while validating page resources.
    return await asyncio.to_thread(check_browser_target, url, policy)


def _forward_browser_headers(raw_headers: Any) -> dict[str, str]:
    """Copy end-to-end browser headers; transport-owned headers are regenerated."""
    if not isinstance(raw_headers, dict):
        return {}
    forwarded: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name)
        value = str(raw_value)
        normalized = name.casefold()
        if (
            normalized in _HOP_BY_HOP_HEADERS
            or normalized in {"host", "content-length"}
            or normalized.startswith(":")
            or "\r" in value
            or "\n" in value
        ):
            continue
        forwarded[name] = value
    return forwarded


async def _fetch_pinned_browser_resource(
    request: dict[str, Any],
    *,
    policy: OrgPolicy,
    timeout_seconds: float,
    max_response_bytes: int = _BROWSER_RESPONSE_MAX_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch a paused Chromium request through OPHIRA's DNS-pinned transport.

    Chromium never connects to the target for requests handled this way. Each
    request (including a browser-followed redirect) is resolved once, validated,
    sent to that numeric address, and fulfilled back into the original page URL.
    """
    request_url = str(request.get("url") or "")
    if not policy.browser_enabled:
        return None, "组织策略已禁用浏览器能力"
    if not domain_allowed(request_url, policy.domain_allowlist):
        return None, "目标域名不在组织 browser domain allowlist 中"

    method = str(request.get("method") or "GET").upper()
    post_data = request.get("postData")
    if post_data is None:
        if request.get("hasPostData"):
            return None, "浏览器请求体不可用，已拒绝不完整转发"
        body = None
    elif isinstance(post_data, str):
        body = post_data.encode("utf-8")
    else:
        return None, "浏览器请求体格式无法安全转发"
    headers = _forward_browser_headers(request.get("headers"))

    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        try:
            response, reason = await send_pinned_public_request(
                client,
                method,
                request_url,
                headers=headers,
                content=body,
                stream=True,
            )
        except httpx.HTTPError as exc:
            return None, f"浏览器目标请求失败：{type(exc).__name__}"
        if response is None:
            return None, reason or "浏览器目标安全校验失败"
        try:
            if response.is_stream_consumed:
                response_body = bytearray(response.content)
            else:
                response_body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(response_body) + len(chunk) > max_response_bytes:
                        return None, "浏览器目标响应超过安全大小限制"
                    response_body.extend(chunk)
            if len(response_body) > max_response_bytes:
                return None, "浏览器目标响应超过安全大小限制"
            response_headers = [
                {"name": name, "value": value}
                for name, value in response.headers.multi_items()
                if name.casefold() not in _HOP_BY_HOP_HEADERS | {"content-length"}
            ]
            response_headers.append(
                {"name": "Content-Length", "value": str(len(response_body))}
            )
            return (
                {
                    "responseCode": response.status_code,
                    "responsePhrase": response.reason_phrase,
                    "responseHeaders": response_headers,
                    "body": base64.b64encode(response_body).decode("ascii"),
                },
                None,
            )
        finally:
            await response.aclose()


async def _install_request_guard(
    context: Any,
    *,
    policy: OrgPolicy,
    baseline_pages: set[Any],
    blocked: list[dict[str, str]],
    guarded_pages: set[Any] | None = None,
):
    """Install a context route while leaving pre-existing user tabs untouched."""

    async def guard(route: Any) -> None:
        request = route.request
        try:
            request_page = request.frame.page
        except Exception:
            request_page = None
        # A popup's first navigation can exist before Playwright exposes its
        # Frame/Page. Fail closed instead of allowing an unguarded redirect
        # chain in a newly opened window.
        if request_page is None and request.is_navigation_request():
            reason = "不允许无法归属到受控页面的新窗口导航"
            blocked.append({"url": request.url, "reason": reason})
            await route.abort("blockedbyclient")
            return
        if request_page in baseline_pages:
            await route.fallback()
            return
        if guarded_pages is not None and request_page not in guarded_pages:
            reason = "不允许未安装连接级安全转发的新窗口请求"
            blocked.append({"url": request.url, "reason": reason})
            await route.abort("blockedbyclient")
            return
        reason = await _check_browser_target(request.url, policy)
        if reason:
            blocked.append({"url": request.url, "reason": reason})
            await route.abort("blockedbyclient")
            return
        # Preserve any route handlers the user's connected browser already has.
        await route.fallback()

    await context.route("**/*", guard)
    return guard


async def _install_cdp_request_guard(
    context: Any,
    page: Any,
    *,
    policy: OrgPolicy,
    blocked: list[dict[str, str]],
    timeout_seconds: float = 30.0,
) -> tuple[Any, set[asyncio.Task[None]], Any]:
    """Relay requests through the pinned host transport before Chromium can send.

    Playwright's high-level ``page.route`` only sees the first URL in a server
    redirect chain. Chromium's Fetch domain reports every subsequent request;
    OPHIRA performs the actual connection to a validated numeric address and
    fulfills the response while the browser remains paused at request stage.
    """
    session = await context.new_cdp_session(page)
    pending: set[asyncio.Task[None]] = set()

    async def handle_request(params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        request = params.get("request") or {}
        request_url = str(request.get("url") or "")
        try:
            fulfilled, reason = await _fetch_pinned_browser_resource(
                request,
                policy=policy,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            fulfilled = None
            reason = f"请求目标安全校验失败：{type(exc).__name__}"

        try:
            if reason or fulfilled is None:
                blocked.append({"url": request_url, "reason": reason})
                await session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            else:
                await session.send(
                    "Fetch.fulfillRequest",
                    {"requestId": request_id, **fulfilled},
                )
        except Exception:
            # If Chromium cannot accept the continue/fail decision, close only
            # this OPHIRA page so detaching the session cannot resume an
            # unvalidated request.
            await _close_page(page)

    def schedule_request(params: dict[str, Any]) -> None:
        task = asyncio.create_task(handle_request(params))
        pending.add(task)
        task.add_done_callback(pending.discard)

    session.on("Fetch.requestPaused", schedule_request)
    try:
        # Route interception does not see requests served by a Service Worker.
        # This target-scoped CDP setting forces this OPHIRA page onto the
        # network path where Fetch.requestPaused can validate it.
        await session.send("Network.setBypassServiceWorker", {"bypass": True})
        await session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        await session.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "http://*/*", "requestStage": "Request"},
                    {"urlPattern": "https://*/*", "requestStage": "Request"},
                    {"urlPattern": "ws://*/*", "requestStage": "Request"},
                    {"urlPattern": "wss://*/*", "requestStage": "Request"},
                ]
            },
        )
    except Exception:
        try:
            await session.detach()
        except Exception:
            pass
        raise
    return session, pending, schedule_request


async def _remove_cdp_request_guard(
    session: Any,
    pending: set[asyncio.Task[None]],
    handler: Any,
) -> None:
    if session is None:
        return
    if pending:
        await asyncio.gather(*tuple(pending), return_exceptions=True)
    try:
        session.remove_listener("Fetch.requestPaused", handler)
    except Exception:
        pass
    try:
        await session.send("Fetch.disable")
    except Exception:
        pass
    try:
        await session.send("Network.setBypassServiceWorker", {"bypass": False})
    except Exception:
        pass
    try:
        await session.send("Network.setCacheDisabled", {"cacheDisabled": False})
    except Exception:
        pass
    try:
        await session.detach()
    except Exception:
        pass


def _install_l2_service_worker_guard(
    context: Any,
    owned_pages: set[Any],
) -> tuple[Any, list[str], set[asyncio.Task[None]]]:
    """Refuse active/new workers without touching the user's tabs or workers."""
    detected: list[str] = []
    close_tasks: set[asyncio.Task[None]] = set()

    def service_worker_started(worker: Any) -> None:
        try:
            label = str(worker.url)
        except Exception:
            label = "active-service-worker"
        detected.append(label)
        # Do not unregister or close a user-owned worker. Closing only the
        # OPHIRA-created page is enough to fail the browser operation closed.
        for candidate in tuple(owned_pages):
            task = asyncio.create_task(_close_page(candidate))
            close_tasks.add(task)
            task.add_done_callback(close_tasks.discard)

    context.on("serviceworker", service_worker_started)
    try:
        existing_workers = tuple(context.service_workers)
    except Exception:
        try:
            context.remove_listener("serviceworker", service_worker_started)
        except Exception:
            pass
        raise
    for worker in existing_workers:
        service_worker_started(worker)
    return service_worker_started, detected, close_tasks


async def _remove_l2_service_worker_guard(
    context: Any,
    handler: Any,
    close_tasks: set[asyncio.Task[None]],
) -> None:
    if context is None or handler is None:
        return
    try:
        context.remove_listener("serviceworker", handler)
    except Exception:
        pass
    if close_tasks:
        await asyncio.gather(*tuple(close_tasks), return_exceptions=True)


def _service_worker_error(count: int) -> dict[str, Any]:
    return _error(
        "BROWSER_SERVICE_WORKER_UNSAFE",
        "连接的 Chrome context 存在 Service Worker；为防止绕过网络策略，已拒绝 L2 操作。",
        active_service_worker_count=count,
    )


async def _close_page(page: Any) -> None:
    try:
        if page is not None and not page.is_closed():
            await page.close()
    except Exception:
        pass


async def _capture_failure_screenshot(
    page: Any,
    screenshot: Path,
    *,
    policy: OrgPolicy,
) -> bool:
    if page is None or page.is_closed():
        return False
    try:
        unsafe = await _check_browser_target(page.url, policy)
    except Exception:
        return False
    if unsafe:
        return False
    try:
        await page.screenshot(path=str(screenshot), full_page=True)
        return True
    except Exception:
        return False


async def _playwright_capture(
    *,
    url: str,
    workspace: Path,
    profile_dir: Path,
    policy: OrgPolicy,
    timeout_seconds: float,
    cdp_endpoint: str | None = None,
    action: str | None = None,
    selector: str | None = None,
    value: str | None = None,
) -> dict[str, Any]:
    """Perform one bounded browser operation with route-level SSRF checks."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return _error(
            "PLAYWRIGHT_NOT_INSTALLED",
            "未安装 Playwright；请安装 backend[browser] 并运行 playwright install chromium。",
        )

    try:
        initial_reason = await _check_browser_target(url, policy)
    except Exception as exc:
        return _error(
            "BROWSER_SECURITY_CHECK_FAILED",
            "无法完成浏览器目标安全校验，已拒绝导航。",
            failure_type=type(exc).__name__,
            url=url,
        )
    if initial_reason:
        return _error("URL_REJECTED", initial_reason, url=url)

    try:
        screenshot = _screenshot_path(workspace)
    except (OSError, ValueError) as exc:
        return _error(
            "BROWSER_WORKSPACE_INVALID",
            "无法创建安全的浏览器证据目录。",
            failure_type=type(exc).__name__,
        )
    timeout_ms = max(1_000, min(int(timeout_seconds * 1_000), 120_000))
    page = None
    context = None
    owns_context = False
    baseline_pages: set[Any] = set()
    owned_pages: set[Any] = set()
    guard = None
    cdp_session = None
    cdp_pending: set[asyncio.Task[None]] = set()
    cdp_handler = None
    service_worker_handler = None
    service_worker_detected: list[str] = []
    service_worker_close_tasks: set[asyncio.Task[None]] = set()
    blocked: list[dict[str, str]] = []

    try:
        async with async_playwright() as playwright:
            if cdp_endpoint:
                # Do not close this Browser/Context: it belongs to the user.
                # Exiting async_playwright disconnects our transport after our
                # own page is closed.
                browser = await playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=timeout_ms,
                )
                if not browser.contexts:
                    return _error(
                        "BROWSER_CONNECTOR_CONTEXT_UNAVAILABLE",
                        "连接的 Chrome 没有可用的默认 context。",
                    )
                context = browser.contexts[0]
            else:
                await asyncio.to_thread(
                    profile_dir.mkdir,
                    parents=True,
                    exist_ok=True,
                    mode=0o700,
                )
                context = await playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=True,
                    accept_downloads=False,
                    service_workers="block",
                )
                owns_context = True

            try:
                if cdp_endpoint:
                    try:
                        (
                            service_worker_handler,
                            service_worker_detected,
                            service_worker_close_tasks,
                        ) = _install_l2_service_worker_guard(context, owned_pages)
                    except Exception as exc:
                        return _error(
                            "BROWSER_SECURITY_GUARD_UNAVAILABLE",
                            "无法检查连接 Chrome 的 Service Worker 状态，已拒绝 L2 操作。",
                            failure_type=type(exc).__name__,
                        )
                    if service_worker_detected:
                        return _service_worker_error(len(service_worker_detected))
                baseline_pages = set(context.pages)
                page = await context.new_page()
                owned_pages.add(page)
                guard = await _install_request_guard(
                    context,
                    policy=policy,
                    baseline_pages=baseline_pages,
                    blocked=blocked,
                    guarded_pages={page},
                )

                def track_popup(popup: Any) -> None:
                    owned_pages.add(popup)

                page.on("popup", track_popup)
                await page.add_init_script(_BLOCK_SERVICE_WORKER_REGISTRATION)
                if service_worker_detected:
                    return _service_worker_error(len(service_worker_detected))
                try:
                    cdp_session, cdp_pending, cdp_handler = (
                        await _install_cdp_request_guard(
                            context,
                            page,
                            policy=policy,
                            blocked=blocked,
                            timeout_seconds=timeout_seconds,
                        )
                    )
                except Exception as exc:
                    return _error(
                        "BROWSER_SECURITY_GUARD_UNAVAILABLE",
                        "无法启用浏览器重定向安全拦截，已拒绝导航。",
                        failure_type=type(exc).__name__,
                    )
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if service_worker_detected:
                    return _service_worker_error(len(service_worker_detected))
                if blocked:
                    captured = await _capture_failure_screenshot(
                        page,
                        screenshot,
                        policy=policy,
                    )
                    return _error(
                        "REQUEST_REJECTED",
                        blocked[-1]["reason"],
                        rejected_url=blocked[-1]["url"],
                        screenshot_path=(
                            str(screenshot.relative_to(workspace)) if captured else None
                        ),
                        final_url=(page.url if captured else None),
                        action=action,
                    )
                if action:
                    assert selector is not None
                    locator = page.locator(selector).first
                    if action == "click":
                        await locator.click(timeout=timeout_ms)
                    elif action == "type":
                        await locator.fill(value or "", timeout=timeout_ms)
                    else:
                        await locator.press("Enter", timeout=timeout_ms)
                    if service_worker_detected:
                        return _service_worker_error(len(service_worker_detected))
                    if blocked:
                        captured = await _capture_failure_screenshot(
                            page,
                            screenshot,
                            policy=policy,
                        )
                        return _error(
                            "REQUEST_REJECTED",
                            blocked[-1]["reason"],
                            rejected_url=blocked[-1]["url"],
                            screenshot_path=(
                                str(screenshot.relative_to(workspace)) if captured else None
                            ),
                            final_url=(page.url if captured else None),
                            action=action,
                        )

                final_url = page.url
                final_reason = await _check_browser_target(final_url, policy)
                if final_reason:
                    return _error(
                        "FINAL_URL_REJECTED",
                        final_reason,
                        rejected_url=final_url,
                    )

                title = await page.title()
                excerpt = await page.inner_text("body")
                await page.screenshot(path=str(screenshot), full_page=True)
                if service_worker_detected:
                    return _service_worker_error(len(service_worker_detected))
                return {
                    "ok": True,
                    "url": url,
                    "final_url": final_url,
                    "title": title,
                    "excerpt": excerpt,
                    "action": action,
                    "screenshot_path": str(screenshot.relative_to(workspace)),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "browser_level": "L2" if cdp_endpoint else "L1",
                    "untrusted": True,
                }
            except Exception as exc:
                if service_worker_detected:
                    return _service_worker_error(len(service_worker_detected))
                if blocked:
                    captured = await _capture_failure_screenshot(
                        page,
                        screenshot,
                        policy=policy,
                    )
                    return _error(
                        "REQUEST_REJECTED",
                        blocked[-1]["reason"],
                        rejected_url=blocked[-1]["url"],
                        screenshot_path=(
                            str(screenshot.relative_to(workspace)) if captured else None
                        ),
                        final_url=(page.url if captured else None),
                        action=action,
                    )
                captured = await _capture_failure_screenshot(page, screenshot, policy=policy)
                return _error(
                    "BROWSER_OPERATION_FAILED",
                    "浏览器操作失败。请检查 selector、页面状态、浏览器安装或连接器健康状态。",
                    failure_type=type(exc).__name__,
                    screenshot_path=(
                        str(screenshot.relative_to(workspace)) if captured else None
                    ),
                    final_url=(page.url if captured else None),
                    action=action,
                )
            finally:
                # Keep network/SW guards active until every OPHIRA-owned page
                # is closed; otherwise restoring SW handling first would leave
                # a short unguarded window in the user's connected context.
                for candidate in tuple(owned_pages):
                    await _close_page(candidate)
                await _remove_l2_service_worker_guard(
                    context,
                    service_worker_handler,
                    service_worker_close_tasks,
                )
                await _remove_cdp_request_guard(
                    cdp_session,
                    cdp_pending,
                    cdp_handler,
                )
                if context is not None and guard is not None:
                    try:
                        await context.unroute("**/*", guard)
                    except Exception:
                        pass
                if owns_context and context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
    except Exception as exc:
        return _error(
            "BROWSER_OPERATION_FAILED",
            "浏览器启动或连接失败。请检查浏览器安装与连接器健康状态。",
            failure_type=type(exc).__name__,
            action=action,
        )


def _record_capture(
    ledger: FileLedger,
    project_id: str,
    run_id: str | None,
    capture: dict[str, Any],
) -> dict[str, Any]:
    relative_path = capture.get("screenshot_path")
    if not isinstance(relative_path, str):
        return capture

    action = capture.get("action")
    final_url = str(capture.get("final_url") or capture.get("url") or "")
    successful = bool(capture.get("ok"))
    activity = f"浏览器动作 {action}" if action else "浏览器页面快照"
    status = "完成" if successful else "失败"
    if run_id is None or not final_url:
        raise SourceReceiptScopeError(
            "browser evidence requires a server-bound run and canonical source"
        )
    source_run = ledger.get_run(run_id)
    if source_run.project_id != project_id or not source_run.sdk_session_id:
        raise SourceReceiptScopeError(
            "browser evidence requires this project's initialized SDK session"
        )
    receipt = ledger.issue_tool_receipt(
        project_id,
        run_id,
        tool=("act_on_page" if action else "snapshot_page"),
        source_type=EvidenceSourceType.TOOL,
        canonical_source=final_url,
        payload=capture,
        object_type="browser_page_capture",
        object_key=final_url,
        object_metadata={
            "title": str(capture.get("title") or f"OPHIRA {activity}截图"),
            "browser_level": str(capture.get("browser_level") or "unknown"),
            "observed_at": str(capture.get("retrieved_at") or datetime.now(UTC).isoformat()),
            "screenshot_path": relative_path,
        },
    )
    evidence = Evidence(
        kind=EvidenceKind.OBSERVED_FACT,
        claim=f"{activity}{status}；截图记录于 {relative_path}",
        source_title=f"OPHIRA {activity}截图",
        source_url=final_url or None,
        source_type=EvidenceSourceType.TOOL,
        source_path=relative_path,
        receipt_id=receipt.id,
        source_run_id=run_id,
        retrieved_at=receipt.retrieved_at,
        observed_at=(
            datetime.fromisoformat(str(capture["retrieved_at"]))
            if capture.get("retrieved_at")
            else datetime.now(UTC)
        ),
        excerpt=capture.get("excerpt"),
        location=f"run:{run_id}" if run_id else "browser-tool",
        freshness_note="浏览器页面内容属于外部不可信数据；截图仅证明当时观察到的页面状态。",
        confidence=1.0,
        supports_hypothesis_ids=[],
        recorded_by=BROWSER_MCP_SERVER_NAME,
        tags=["browser", "screenshot", *([f"run:{run_id}"] if run_id else [])],
    )
    ledger.update_project(project_id, lambda project: project.evidence.append(evidence))
    artifact = ledger.register_artifact(
        project_id,
        relative_path,
        run_id=run_id,
        source_ids=[evidence.id],
        mime="image/png",
    )
    return {**capture, "artifact_id": artifact.id, "evidence_id": evidence.id}


def _sanitize_capture(capture: dict[str, Any]) -> dict[str, Any]:
    """Apply the shared external-data boundary before ledger or MCP exposure."""
    external_fields = {
        key: capture[key]
        for key in ("title", "excerpt")
        if isinstance(capture.get(key), str)
    }
    if not external_fields:
        return capture
    sanitized = sanitize_untrusted_payload(external_fields, UntrustedSource.BROWSER_PAGE)
    result = {
        **capture,
        **sanitized.value,
        "untrusted": True,
        "truncated": bool(capture.get("truncated")) or sanitized.truncated,
    }
    result["note"] = UNTRUSTED_DATA_NOTICE
    return {"note": UNTRUSTED_DATA_NOTICE, **result}


def build_browser_tools(
    ledger: FileLedger,
    project_id: str,
    settings: Any,
    *,
    connectors: ConnectorRegistry | None = None,
    policy: OrgPolicy | None = None,
    run_id: str | None = None,
    project_events: ProjectEventProjector | None = None,
    shop_browsers: ShopBrowserService | None = None,
):
    workspace = ledger.workspace_for_project(project_id)
    profile_dir = ledger.root / "browser-profiles" / project_id / "l1"
    org_policy = policy or OrgPolicy()

    async def run_capture(args: dict[str, Any], *, action: str | None = None) -> dict[str, Any]:
        if not org_policy.browser_enabled:
            return _result(
                _error("BROWSER_DISABLED_BY_POLICY", "组织策略已禁用浏览器能力。"),
                is_error=True,
            )
        if args.get("shop_id") or args.get("list_shops") is True:
            from .auth import AuthorizationError
            from .shop_browser_store import ShopBrowserError, ShopBrowserOperation

            try:
                if shop_browsers is None or run_id is None or args.get("use_connected_browser"):
                    raise ValueError("shop browser unavailable")
                shop_browsers.authority._run(project_id, run_id)
                if args.get("list_shops") is True:
                    if args.get("shop_id") or args.get("url") or action:
                        raise ValueError("incompatible shop listing arguments")
                    offset = args.get("shop_offset", 0)
                    if type(offset) is not int or not 0 <= offset <= 100:
                        raise ValueError("invalid shop offset")
                    shops = shop_browsers.list(project_id)
                    fields = {
                        "id",
                        "name",
                        "platform",
                        "seller_shop_id",
                        "connector_id",
                        "entry_url",
                        "browser_state",
                        "agent_access",
                        "grant_expires_at",
                    }
                    return _result(
                        {
                            "shops": [
                                {key: value for key, value in shop.items() if key in fields}
                                for shop in shops[offset : offset + 10]
                            ],
                            "next_offset": offset + 10 if offset + 10 < len(shops) else None,
                        }
                    )
                operation = ShopBrowserOperation(
                    operation=action or ("navigate" if args.get("url") else "snapshot"),
                    url=args.get("url") if not action else None,
                    observation_id=args.get("observation_id"),
                    target=args.get("selector"),
                    value=args.get("value"),
                )
                capture = await shop_browsers.capture(
                    project_id, run_id, args["shop_id"], operation
                )
                if project_events is not None:
                    await project_events.reconcile_run(run_id)
                return _result(capture)
            except ShopBrowserError as exc:
                return _result(_error(exc.code, str(exc)), is_error=True)
            except (AuthorizationError, ValueError, OSError):
                return _result(
                    _error("SHOP_ACCESS_UNAVAILABLE", "Project shop access is unavailable."),
                    is_error=True,
                )
        if action == "download" or not args.get("url"):
            return _result(
                _error("BROWSER_INPUT_INVALID", "Downloads require a configured shop session."),
                is_error=True,
            )
        cdp_endpoint = None
        if args.get("use_connected_browser") is True:
            if connectors is None:
                return _result(
                    _error("BROWSER_CONNECTOR_NOT_CONFIGURED", "Chrome CDP connector 尚未配置。"),
                    is_error=True,
                )
            try:
                cdp_endpoint = connectors.require_healthy_browser_endpoint()
            except ConnectorUnavailableError as exc:
                return _result(_error(exc.code, str(exc)), is_error=True)
            except Exception as exc:
                return _result(
                    _error(
                        "BROWSER_CONNECTOR_ERROR",
                        "无法安全读取 Chrome CDP connector。",
                        failure_type=type(exc).__name__,
                    ),
                    is_error=True,
                )

        capture = await _playwright_capture(
            url=args["url"],
            workspace=workspace,
            profile_dir=profile_dir,
            policy=org_policy,
            timeout_seconds=settings.web_timeout_seconds,
            cdp_endpoint=cdp_endpoint,
            action=action,
            selector=args.get("selector"),
            value=args.get("value"),
        )
        capture = _sanitize_capture(capture)
        if capture.get("screenshot_path"):
            try:
                capture = _record_capture(ledger, project_id, run_id, capture)
            except Exception as exc:
                capture = _error(
                    "EVIDENCE_REGISTRATION_FAILED",
                    "截图已生成，但 Artifact/Evidence 落账失败。",
                    failure_type=type(exc).__name__,
                    screenshot_path=capture.get("screenshot_path"),
                )
            finally:
                if project_events is not None and run_id is not None:
                    # Artifact registration may fail after Evidence committed.
                    # Project truth, not the combined tool result, drives notices.
                    await project_events.reconcile_run(run_id)
        return _result(capture, is_error=not bool(capture.get("ok")))

    @tool(
        "snapshot_page",
        "Read a public page in OPHIRA's isolated browser (L1), or set "
        "use_connected_browser=true to use the approved Chrome connector (L2). "
        "The model cannot provide a CDP endpoint. Set list_shops=true (shop_offset pagination) "
        "to discover this project's configured shops. Use shop_id to read its authorized, "
        "independent login session; optional url navigates within its permitted domains. "
        "Shop snapshots return observation_id and element_N references for act_on_page. "
        "Save screenshots, text and reports as source-linked project data. Stop at sign-in walls.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 2000},
                "use_connected_browser": {"type": "boolean", "default": False},
                "shop_id": {"type": "string", "pattern": "^shop_[a-f0-9]{32}$"},
                "list_shops": {"type": "boolean", "default": False},
                "shop_offset": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "anyOf": [{"required": ["url"]}, {"required": ["shop_id"]},
                      {"required": ["list_shops"], "properties": {"list_shops": {"const": True}}}],
            "additionalProperties": False,
        },
    )
    async def snapshot_page(args: dict[str, Any]) -> dict[str, Any]:
        return await run_capture(args)

    @tool(
        "act_on_page",
        "Click, type, submit, or download a report in the policy-controlled browser. "
        "For a project shop use shop_id, observation_id from snapshot_page, and its observed "
        "element_N as selector; do not invent selectors or automate sign-in. Always requires user "
        "approval. The model cannot provide a CDP endpoint. Every attempt stores a "
        "screenshot when safe and registers it as Artifact/Evidence.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 2000},
                "action": {"type": "string", "enum": ["click", "type", "submit", "download"]},
                "selector": {"type": "string", "minLength": 1, "maxLength": 300},
                "value": {"type": ["string", "null"], "maxLength": 2000},
                "use_connected_browser": {"type": "boolean", "default": False},
                "shop_id": {"type": "string", "pattern": "^shop_[a-f0-9]{32}$"},
                "observation_id": {"type": "string", "pattern": "^page_[a-f0-9]{32}$"},
            },
            "required": ["action", "selector"],
            "anyOf": [{"required": ["url"]}, {"required": ["shop_id", "observation_id"]}],
            "additionalProperties": False,
        },
    )
    async def act_on_page(args: dict[str, Any]) -> dict[str, Any]:
        return await run_capture(args, action=args["action"])

    return [snapshot_page, act_on_page]


def build_browser_mcp_server(
    ledger: FileLedger,
    project_id: str,
    settings: Any,
    *,
    connectors: ConnectorRegistry | None = None,
    policy: OrgPolicy | None = None,
    run_id: str | None = None,
    project_events: ProjectEventProjector | None = None,
    shop_browsers: ShopBrowserService | None = None,
):
    return create_sdk_mcp_server(
        name=BROWSER_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_browser_tools(
            ledger,
            project_id,
            settings,
            connectors=connectors,
            policy=policy,
            run_id=run_id,
            project_events=project_events,
            shop_browsers=shop_browsers,
        ),
    )
