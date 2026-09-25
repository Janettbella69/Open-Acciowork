#!/usr/bin/env python3
"""Exercise the real HTTP 307 preview hop and module imports in Chromium."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import Response
from playwright.async_api import Route, async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from run_site_publication_browser_e2e import (  # noqa: E402
    SECRET,
    FakeRuntime,
    gateway_headers,
    module_zip,
)

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402


async def run() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="ophira-preview-redirect-") as temporary:
        app = create_app(Settings(
            data_dir=Path(temporary), auth_mode="trusted_gateway",
            trusted_gateway_auth_secret=SECRET,
            trusted_gateway_tenant_id="tenant_sites",
            site_public_origin="https://pages.example.com",
        ), runtime=FakeRuntime())
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin,
        ) as client:
            async def managed(method: str, path: str, **kwargs):
                response = await client.request(
                    method, path, headers=gateway_headers(method, path), **kwargs,
                )
                response.raise_for_status()
                return response

            created = await managed("POST", "/api/projects", json={
                "name": "Module redirect", "objective": "Review a site draft",
            })
            project_id = created.json()["id"]
            workspace = app.state.ledger.workspace_for_project(project_id)
            (workspace / "module.zip").write_bytes(module_zip())
            artifact = app.state.ledger.register_artifact(project_id, "module.zip")
            base = f"/api/projects/{project_id}/site-publications"
            staged = await managed("POST", base, json={
                "artifact_id": artifact.id, "allow_scripts": True,
            })
            site_id = staged.json()["publication"]["id"]
            preview_path = f"{base}/{site_id}/preview/"

        @app.get("/preview-container")
        def preview_container():
            return Response(
                f'<iframe id="preview" sandbox="allow-scripts" '
                f'src="{preview_path}"></iframe>',
                media_type="text/html",
            )

        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        ))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if server_task.done():
                    raise RuntimeError("Preview HTTP server stopped before startup")
                await asyncio.sleep(0.05)
            if not server.started:
                raise RuntimeError("Preview HTTP server did not start")

            origins: list[str | None] = []
            cookies: list[str | None] = []
            statuses: list[int] = []

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(service_workers="block")
                    await context.add_cookies([{
                        "name": "control_session", "value": "browser-only",
                        "url": origin, "sameSite": "Lax",
                    }])

                    async def route_request(route: Route) -> None:
                        path = urlsplit(route.request.url).path
                        request_headers = await route.request.all_headers()
                        if path == preview_path:
                            headers = dict(request_headers)
                            headers.update(gateway_headers("GET", preview_path))
                            await route.continue_(headers=headers)
                            return
                        if path.startswith("/api/site-previews/") and "/assets/" in path:
                            origins.append(request_headers.get("origin"))
                            cookies.append(request_headers.get("cookie"))
                        await route.continue_()

                    await context.route("**/*", route_request)
                    page = await context.new_page()
                    page.on("response", lambda response: statuses.append(response.status)
                            if urlsplit(response.url).path == preview_path else None)
                    await page.goto(origin + "/preview-container")
                    frame = page.frame_locator("#preview")
                    await frame.get_by_text("Module ready").wait_for(timeout=10_000)
                    preview_frame = next(
                        item for item in page.frames
                        if urlsplit(item.url).path.startswith("/api/site-previews/")
                    )
                    return {
                        "real_http_redirect_reaches_capability": (
                            307 in statuses and preview_frame.url.startswith(origin)
                        ),
                        "module_import_runs_without_cookie": (
                            await frame.locator("#module-state").inner_text() == "Module ready"
                            and len(origins) == 2 and all(value == "null" for value in origins)
                            and all(value is None for value in cookies)
                        ),
                    }
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await server_task
            listener.close()


if __name__ == "__main__":
    checks = asyncio.run(run())
    print(json.dumps(checks, indent=2, sort_keys=True))
    if not all(checks.values()):
        raise SystemExit(1)
