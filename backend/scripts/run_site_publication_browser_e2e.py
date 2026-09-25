#!/usr/bin/env python3
"""Exercise static and opted-in interactive publications in real Chromium.

An ASGI app supplies the real publication responses through Playwright routing.
No public DNS, gateway account, or listening socket is needed.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.async_api import Route, async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from ophira_backend.auth import build_gateway_assertion_headers  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import PrincipalRole  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402

SECRET = "site-publication-browser-gateway-secret-0001"
HTML = b"""<!doctype html><html><body>
<button id="change">Change text</button><p id="state">Waiting</p>
<form action="https://collector.example/submit" method="get">
  <button id="submit" type="submit">Submit</button>
</form>
<script src="https://collector.example/remote.js"></script>
<script>
document.querySelector('#change').onclick = () => {
  document.querySelector('#state').textContent = 'Interactive';
};
try { localStorage.setItem('published', 'yes'); window.storageResult = 'allowed'; }
catch (_) { window.storageResult = 'blocked'; }
try { document.cookie = 'published=yes'; window.cookieResult = document.cookie || 'blocked'; }
catch (_) { window.cookieResult = 'blocked'; }
fetch('https://collector.example/secret').then(
  () => window.networkResult = 'allowed',
  () => window.networkResult = 'blocked',
);
const pixel = new Image();
pixel.src = 'https://collector.example/pixel';
document.body.append(pixel);
</script></body></html>"""
BUNDLE_HTML = b"""<!doctype html><html><head>
<link rel="stylesheet" href="assets/site.css"></head><body>
<h1 id="bundle-state">Waiting</h1><img id="bundle-image" src="assets/logo.png" alt="Logo">
<button id="bundle-change">Change</button>
<script src="assets/site.js"></script></body></html>"""
BUNDLE_JS = b"""document.querySelector('#bundle-change').onclick = () => {
document.querySelector('#bundle-state').textContent = 'Bundled';
};"""
BUNDLE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f"
    b"\x00\x03\x03\x02\x00\xef\xbf\xca\xa9\x00\x00\x00\x00IEND\xaeB`\x82"
)


def bundle_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", BUNDLE_HTML)
        archive.writestr("assets/site.css", b"h1 { color: rgb(0, 128, 0); }")
        archive.writestr("assets/site.js", BUNDLE_JS)
        archive.writestr("assets/logo.png", BUNDLE_PNG)
    return output.getvalue()


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def gateway_headers(method: str, path: str) -> dict[str, str]:
    return build_gateway_assertion_headers(
        SECRET, method=method, path=path, tenant_id="tenant_sites",
        principal_id="owner@example.com", role=PrincipalRole.OWNER,
        session_id="site_browser_session", issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )


async def run() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="ophira-site-browser-") as temporary:
        app = create_app(Settings(
            data_dir=Path(temporary), auth_mode="trusted_gateway",
            trusted_gateway_auth_secret=SECRET,
            trusted_gateway_tenant_id="tenant_sites",
            site_public_origin="https://pages.example.com",
        ), runtime=FakeRuntime())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.com",
        ) as client:
            async def managed(method: str, path: str, **kwargs):
                response = await client.request(
                    method, path, headers=gateway_headers(method, path), **kwargs,
                )
                response.raise_for_status()
                return response

            created = await managed("POST", "/api/projects", json={
                "name": "Browser publication", "objective": "Interactive page",
            })
            project_id = created.json()["id"]
            workspace = app.state.ledger.workspace_for_project(project_id)
            base = f"/api/projects/{project_id}/site-publications"
            urls: dict[str, str] = {}
            previews: dict[str, str] = {}
            for label, allow_scripts in (("static", False), ("interactive", True)):
                path = workspace / f"{label}.html"
                path.write_bytes(HTML)
                artifact = app.state.ledger.register_artifact(project_id, path.name)
                staged = await managed("POST", base, json={
                    "artifact_id": artifact.id, "allow_scripts": allow_scripts,
                })
                item = staged.json()["publication"]
                previews[label] = f"{base}/{item['id']}/preview"
                approved = await managed("POST", f"{base}/{item['id']}/publish", json={
                    "expected_revision": 1, "expected_checksum": artifact.checksum,
                    "expected_allow_scripts": allow_scripts, "confirmation": "publish",
                })
                urls[label] = approved.json()["public_url"]

            (workspace / "bundle.zip").write_bytes(bundle_zip())
            bundle_artifact = app.state.ledger.register_artifact(project_id, "bundle.zip")
            bundle_stage = await managed("POST", base, json={
                "artifact_id": bundle_artifact.id, "allow_scripts": True,
            })
            bundle_item = bundle_stage.json()["publication"]
            previews["bundle"] = f"{base}/{bundle_item['id']}/preview/"
            bundle_approved = await managed(
                "POST", f"{base}/{bundle_item['id']}/publish", json={
                    "expected_revision": 1, "expected_checksum": bundle_artifact.checksum,
                    "expected_allow_scripts": True, "confirmation": "publish",
                },
            )
            urls["bundle"] = bundle_approved.json()["public_url"]
            static_bundle_stage = await managed("POST", base, json={
                "artifact_id": bundle_artifact.id, "allow_scripts": False,
            })
            static_bundle_item = static_bundle_stage.json()["publication"]
            static_bundle_approved = await managed(
                "POST", f"{base}/{static_bundle_item['id']}/publish", json={
                    "expected_revision": 1, "expected_checksum": bundle_artifact.checksum,
                    "expected_allow_scripts": False, "confirmation": "publish",
                },
            )
            urls["bundle_static"] = static_bundle_approved.json()["public_url"]

            blocked_requests: list[str] = []

            async def route_request(route: Route) -> None:
                url = route.request.url
                parsed = urlsplit(url)
                if parsed.hostname not in {"pages.example.com", "gateway.example.com"}:
                    blocked_requests.append(url)
                    await route.abort()
                    return
                if parsed.path in {"/preview-container", "/bundle-preview-container"}:
                    preview_key = (
                        "bundle" if parsed.path == "/bundle-preview-container" else "interactive"
                    )
                    html = (
                        '<iframe id="preview" sandbox="allow-scripts" '
                        f'src="{previews[preview_key]}"></iframe>'
                    )
                    await route.fulfill(status=200, content_type="text/html", body=html)
                    return
                headers = (
                    gateway_headers("GET", parsed.path)
                    if parsed.hostname == "gateway.example.com" else {}
                )
                response = await client.get(url, headers=headers)
                await route.fulfill(
                    status=response.status_code, headers=dict(response.headers),
                    body=response.content,
                )

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(service_workers="block")
                    await context.route("**/*", route_request)
                    page = await context.new_page()

                    await page.goto(urls["static"])
                    await page.locator("#change").click()
                    static_inert = (
                        await page.locator("#state").inner_text() == "Waiting"
                        and await page.evaluate("window.storageResult === undefined")
                    )

                    await page.goto(urls["interactive"])
                    await page.locator("#change").click()
                    await page.wait_for_function("window.networkResult === 'blocked'")
                    interactive_runs = await page.locator("#state").inner_text() == "Interactive"
                    storage_blocked = await page.evaluate(
                        "window.storageResult === 'blocked' && window.cookieResult === 'blocked'"
                    )
                    network_blocked = await page.evaluate("window.networkResult === 'blocked'")
                    await page.locator("#submit").click()
                    form_blocked = page.url == urls["interactive"]

                    await page.goto("https://gateway.example.com/preview-container")
                    frame = page.frame_locator("#preview")
                    await frame.locator("#change").click()
                    preview_runs = await frame.locator("#state").inner_text() == "Interactive"
                    preview_isolated = await frame.locator("body").evaluate(
                        "() => { try { return window.parent.document.body === null; } "
                        "catch (_) { return true; } }"
                    )

                    await page.goto(urls["bundle"])
                    await page.locator("#bundle-change").click()
                    bundle_script = await page.locator("#bundle-state").inner_text() == "Bundled"
                    bundle_css = await page.locator("#bundle-state").evaluate(
                        "element => getComputedStyle(element).color === 'rgb(0, 128, 0)'"
                    )
                    bundle_image = await page.locator("#bundle-image").evaluate(
                        "element => element.complete && element.naturalWidth === 1"
                    )

                    await page.goto(urls["bundle_static"])
                    await page.locator("#bundle-change").click()
                    bundle_script_inert = (
                        await page.locator("#bundle-state").inner_text() == "Waiting"
                    )

                    await page.goto("https://gateway.example.com/bundle-preview-container")
                    bundle_frame = page.frame_locator("#preview")
                    await bundle_frame.locator("#bundle-change").click()
                    bundle_preview_script = (
                        await bundle_frame.locator("#bundle-state").inner_text() == "Bundled"
                    )
                    bundle_preview_css = await bundle_frame.locator("#bundle-state").evaluate(
                        "element => getComputedStyle(element).color === 'rgb(0, 128, 0)'"
                    )
                    bundle_preview_image = await bundle_frame.locator("#bundle-image").evaluate(
                        "element => element.complete && element.naturalWidth === 1"
                    )
                finally:
                    await browser.close()

            return {
                "previously_static_script_remains_inert": static_inert,
                "interactive_page_runs_inline_script": interactive_runs,
                "interactive_storage_and_cookie_blocked": storage_blocked,
                "interactive_network_blocked": network_blocked,
                "interactive_form_submission_blocked": form_blocked,
                "preview_runs_matching_script": preview_runs,
                "preview_cannot_read_parent": preview_isolated,
                "bundled_script_runs": bundle_script,
                "bundled_css_renders": bundle_css,
                "bundled_image_renders": bundle_image,
                "bundle_script_inert_without_approval": bundle_script_inert,
                "bundle_preview_script_runs": bundle_preview_script,
                "bundle_preview_css_renders": bundle_preview_css,
                "bundle_preview_image_renders": bundle_preview_image,
                "no_external_requests_reached_transport": not blocked_requests,
            }


if __name__ == "__main__":
    checks = asyncio.run(run())
    print(json.dumps(checks, indent=2, sort_keys=True))
    if not all(checks.values()):
        raise SystemExit(1)
