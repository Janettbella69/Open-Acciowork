"""Private loopback ingress for the native desktop host.

The existing local-only API intentionally has no user authentication. A desktop
webview therefore needs an additional, process-scoped bearer boundary before
any API, health, or static response is reachable from another local process.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from .desktop_bridge import DesktopBridge

DESKTOP_COOKIE_NAME = "ophira_desktop_session"
DESKTOP_HEALTH_HEADER = "x-ophira-desktop-token"
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class DesktopHost:
    """Serve the built UI and guard the existing API with a private session."""

    def __init__(
        self,
        api: ASGIApp,
        *,
        ui_dir: Path,
        token: str,
        port: int,
        bridge: DesktopBridge | None = None,
    ) -> None:
        if not _TOKEN_PATTERN.fullmatch(token):
            raise ValueError("desktop session token must be 32 random bytes in hex")
        if not 1 <= port <= 65_535:
            raise ValueError("desktop port is invalid")
        ui_dir = ui_dir.resolve(strict=True)
        index = ui_dir / "index.html"
        assets = ui_dir / "assets"
        if (
            not index.is_file() or index.is_symlink()
            or not assets.is_dir() or assets.is_symlink()
        ):
            raise ValueError("desktop UI build is incomplete")
        self.api = api
        self.token = token
        self.origin = f"http://127.0.0.1:{port}"
        self.host = f"127.0.0.1:{port}"
        self.ui_dir = ui_dir
        self.bridge = bridge
        self.static = Starlette(routes=[Mount("/", app=StaticFiles(directory=ui_dir, html=True))])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.api(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope)
        client = request.client
        try:
            local_client = client is not None and ipaddress.ip_address(client.host).is_loopback
        except ValueError:
            local_client = False
        if (
            not local_client
            or request.url.scheme != "http"
            or request.headers.get("host") != self.host
            or (request.headers.get("origin") not in (None, self.origin))
            or request.headers.get("sec-fetch-site") not in (None, "none", "same-origin")
        ):
            await self._denied(scope, receive, send)
            return
        cookie = request.cookies.get(DESKTOP_COOKIE_NAME, "")
        health_header = (
            request.headers.get(DESKTOP_HEALTH_HEADER, "")
            if request.url.path == "/health" else ""
        )
        if not (
            hmac.compare_digest(cookie, self.token)
            or (health_header and hmac.compare_digest(health_header, self.token))
        ):
            await self._denied(scope, receive, send)
            return

        path = request.url.path
        if path == "/api/desktop/status" and self.bridge is not None:
            from .desktop_bridge import DesktopBridgeError

            if request.method != "GET":
                response = JSONResponse({"detail": "method not allowed"}, status_code=405)
            else:
                try:
                    result = await self.bridge.request("status", timeout_seconds=5)
                    response = JSONResponse(result, headers={"Cache-Control": "no-store"})
                except DesktopBridgeError:
                    response = JSONResponse(
                        {"detail": "native desktop host is unavailable"},
                        status_code=503,
                        headers={"Cache-Control": "no-store"},
                    )
            await response(scope, receive, send)
        elif path == "/app" or path.startswith("/app/"):
            response = FileResponse(
                self.ui_dir / "index.html",
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            await response(scope, receive, send)
        elif path == "/" or path.startswith("/assets/") or (
            path.count("/") == 1 and (self.ui_dir / path.lstrip("/")).is_file()
        ):
            await self.static(scope, receive, send)
        else:
            await self.api(scope, receive, send)

    @staticmethod
    async def _denied(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "desktop session is unavailable"},
            status_code=401,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
        await response(scope, receive, send)
