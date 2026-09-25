#!/usr/bin/env python3
"""Exercise L1 redirect, resource, Service Worker, and recovery guards in real Chromium.

Chromium and its CDP request interception are real. The public target transport is
replaced with a fixed in-process fixture so this test needs no external account or
network. DNS pinning and private-address rejection have separate contract tests.

Run: .venv/bin/python backend/scripts/run_browser_security_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend import browser_tools  # noqa: E402
from ophira_backend.browser_tools import _playwright_capture, domain_allowed  # noqa: E402
from ophira_backend.models import OrgPolicy  # noqa: E402

HOST = "fixture.example"
ORIGIN = f"https://{HOST}"
REDIRECT_TARGET = "http://127.0.0.1:41273/private"


def fixture_response(client: httpx.AsyncClient, method: str, url: str) -> httpx.Response:
    path = urlsplit(url).path
    request = client.build_request(method, url)
    if path == "/redirect-private":
        return httpx.Response(
            302, headers={"Location": REDIRECT_TARGET}, request=request,
        )
    if path == "/redirect-local":
        return httpx.Response(
            302, headers={"Location": f"{ORIGIN}/ok"}, request=request,
        )
    if path == "/blocked-script":
        return httpx.Response(
            200, headers={"Content-Type": "text/html"},
            content=b'<html><head><script src="https://denied.example/payload.js"></script>'
            b'</head><body>Blocked script fixture</body></html>',
            request=request,
        )
    if path == "/service-worker":
        return httpx.Response(
            200, headers={"Content-Type": "text/html"},
            content=b"""<html><body>Waiting for worker result<script type="module">
                const workerType = typeof ServiceWorkerContainer;
                try {
                  const registration = await navigator.serviceWorker.register('/sw.js');
                  document.body.textContent = registration
                    ? `worker registered:${workerType}` : 'worker unavailable';
                } catch (error) {
                  document.body.textContent = `worker unavailable:${error.name}`;
                }
            </script></body></html>""",
            request=request,
        )
    if path == "/ok":
        return httpx.Response(
            200, headers={"Content-Type": "text/html"},
            content=b"<html><head><title>Recovered</title></head>"
            b"<body>Recovery succeeded</body></html>",
            request=request,
        )
    raise AssertionError(f"Unexpected fixture request: {url}")


async def run() -> dict:
    calls: list[str] = []
    original_check = browser_tools._check_browser_target
    original_send = browser_tools.send_pinned_public_request
    # Deliberately include a loopback host in the policy so the lower SSRF
    # boundary, not merely the domain allowlist, must stop that redirect.
    policy = OrgPolicy(browser_enabled=True, domain_allowlist=[HOST, "127.0.0.1"])

    async def check_fixture_target(url: str, current_policy: OrgPolicy) -> str | None:
        # Keep real policy matching; only the synthetic public fixture origin
        # bypasses DNS because no external DNS/network is needed here.
        if (
            urlsplit(url).scheme == "https"
            and urlsplit(url).hostname == HOST
            and current_policy.browser_enabled
            and domain_allowed(url, current_policy.domain_allowlist)
        ):
            return None
        if not domain_allowed(url, current_policy.domain_allowlist):
            return "target domain is outside the browser allowlist"
        return await original_check(url, current_policy)

    async def send_fixture(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> tuple[httpx.Response | None, str | None]:
        if urlsplit(url).hostname != HOST:
            reason = browser_tools.check_public_http_url(url)
            if reason:
                return None, reason
            raise AssertionError(f"Unexpected public host reached the fixture transport: {url}")
        calls.append(url)
        return fixture_response(client, method, url), None

    browser_tools._check_browser_target = check_fixture_target
    browser_tools.send_pinned_public_request = send_fixture
    try:
        with tempfile.TemporaryDirectory(prefix="ophira-browser-security-") as temporary:
            root = await asyncio.to_thread(Path(temporary).resolve)
            workspace = root / "workspace"
            workspace.mkdir()
            profile = root / "profile"

            async def capture(path: str, cdp_endpoint: str | None = None) -> dict:
                return await _playwright_capture(
                    url=f"{ORIGIN}{path}", workspace=workspace,
                    profile_dir=profile, policy=policy, timeout_seconds=15,
                    cdp_endpoint=cdp_endpoint,
                )

            worker = await capture("/service-worker")
            script = await capture("/blocked-script")
            private = await capture("/redirect-private")
            local = await capture("/redirect-local")
            recovered = await capture("/ok")
            l2_results: dict[str, dict] = {}
            baseline_ok = False
            async with async_playwright() as playwright:
                cdp_profile = root / "cdp-profile"
                primary_context = await playwright.chromium.launch_persistent_context(
                    str(cdp_profile), headless=True,
                    args=[
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=0",
                        "--disable-background-networking",
                    ],
                )
                try:
                    active_port = cdp_profile / "DevToolsActivePort"
                    for _ in range(100):
                        if await asyncio.to_thread(active_port.exists):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise TimeoutError("Dedicated Chrome did not expose its CDP port")
                    port = int((await asyncio.to_thread(active_port.read_text)).splitlines()[0])
                    endpoint = f"http://127.0.0.1:{port}"
                    baseline = await primary_context.new_page()
                    await baseline.set_content("<title>Unowned tab</title><body>Unowned</body>")

                    l2_results["script"] = await capture("/blocked-script", endpoint)
                    l2_results["private"] = await capture("/redirect-private", endpoint)
                    l2_results["local"] = await capture("/redirect-local", endpoint)
                    l2_results["recovered"] = await capture("/ok", endpoint)
                    baseline_ok = (
                        not baseline.is_closed()
                        and await baseline.title() == "Unowned tab"
                        and await baseline.inner_text("body") == "Unowned"
                    )
                finally:
                    await primary_context.close()

            checks = {
                "worker_registration_refused": (
                    worker.get("ok") is True
                    and "worker unavailable" in worker.get("excerpt", "")
                    and f"{ORIGIN}/sw.js" not in calls
                ),
                "denied_script_blocked": (
                    script.get("error_code") == "REQUEST_REJECTED"
                    and script.get("rejected_url") == "https://denied.example/payload.js"
                ),
                "private_redirect_blocked": (
                    private.get("error_code") == "REQUEST_REJECTED"
                    and private.get("rejected_url") == REDIRECT_TARGET
                    and "SSRF" in private.get("error", "")
                ),
                "same_origin_redirect_allowed": (
                    local.get("ok") is True and local.get("final_url") == f"{ORIGIN}/ok"
                ),
                "profile_recovered_after_rejections": (
                    recovered.get("ok") is True
                    and recovered.get("title") == "Recovered"
                    and (workspace / recovered.get("screenshot_path", "missing")).is_file()
                ),
                "transport_never_saw_denied_target": all(
                    urlsplit(url).hostname == HOST for url in calls
                ),
                "l2_denied_script_blocked": (
                    l2_results["script"].get("error_code") == "REQUEST_REJECTED"
                    and l2_results["script"].get("rejected_url")
                    == "https://denied.example/payload.js"
                ),
                "l2_private_redirect_blocked": (
                    l2_results["private"].get("error_code") == "REQUEST_REJECTED"
                    and l2_results["private"].get("rejected_url") == REDIRECT_TARGET
                    and "SSRF" in l2_results["private"].get("error", "")
                ),
                "l2_same_origin_redirect_allowed": (
                    l2_results["local"].get("ok") is True
                    and l2_results["local"].get("final_url") == f"{ORIGIN}/ok"
                ),
                "l2_recovered_and_preserved_user_tab": (
                    l2_results["recovered"].get("ok") is True and baseline_ok
                ),
            }
            return {
                "kind": "browser_l1_l2_security_e2e",
                "passed": all(checks.values()),
                "checks": checks,
                "result_codes": {
                    "worker": worker.get("error_code", "ok"),
                    "script": script.get("error_code", "ok"),
                    "private": private.get("error_code", "ok"),
                    "local": local.get("error_code", "ok"),
                    "recovered": recovered.get("error_code", "ok"),
                },
                "failure_types": {
                    name: result.get("failure_type")
                    for name, result in (
                        ("worker", worker), ("script", script), ("private", private),
                        ("local", local), ("recovered", recovered),
                    ) if result.get("failure_type")
                },
                "fixture_requests": len(calls),
                "l2_result_codes": {
                    name: result.get("error_code", "ok") for name, result in l2_results.items()
                },
            }
    finally:
        browser_tools._check_browser_target = original_check
        browser_tools.send_pinned_public_request = original_send


if __name__ == "__main__":
    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
