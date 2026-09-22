"""Loopback-only recovery UI. Never constructs the application or starts jobs."""

import argparse
import re
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from .backup_files import BackupError
from .maintenance import DataDirectoryBusyError
from .maintenance_console_models import MaintenanceConfirm, MaintenancePrepare
from .maintenance_console_store import MaintenanceConsole
from .request_bodies import read_bounded_body

SESSION_SECONDS = 8 * 60 * 60


def create_console_app(service, *, token, origin, assets, clock=time.monotonic):
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token)
    ):
        raise ValueError("maintenance console requires loopback origin and private capability")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    expires = clock() + SESSION_SECONDS

    def authorize():
        if clock() >= expires:
            raise HTTPException(403, "maintenance_access_denied")

    @app.middleware("http")
    async def guard(request, call_next):
        local = request.client and request.client.host in {"127.0.0.1", "::1"}
        same_origin = request.headers.get("origin") in {None, origin}
        valid = local and request.headers.get("host") == parsed.netloc and same_origin
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "")
            valid = (
                valid and clock() < expires and secrets.compare_digest(supplied, f"Bearer {token}")
            )
            if request.method not in {"GET", "HEAD"}:
                valid = valid and request.headers.get("origin") == origin
        response = (
            await call_next(request)
            if valid
            else JSONResponse({"detail": "maintenance_access_denied"}, status_code=403)
        )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                ),
            }
        )
        return response

    async def invoke(operation):
        try:
            result = await run_in_threadpool(operation)
            authorize()
            return result
        except DataDirectoryBusyError as exc:
            raise HTTPException(409, "maintenance_directory_busy") from exc
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, BackupError) and re.fullmatch(r"[a-z0-9_]{1,100}", str(exc))
                else "maintenance_validation_failed"
            )
            raise HTTPException(409, reason) from exc

    async def payload(request, model):
        raw = await read_bounded_body(
            request, max_bytes=2048, too_large_detail="maintenance_request_too_large"
        )
        try:
            return model.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "invalid_maintenance_request") from exc

    @app.get("/api/maintenance")
    async def status():
        return await invoke(service.status)

    @app.post("/api/maintenance/reviews")
    async def prepare(request: Request):
        data = await payload(request, MaintenancePrepare)
        return await invoke(lambda: service.prepare(data))

    @app.post("/api/maintenance/reviews/{request_id}/confirm")
    async def confirm(request_id: str, request: Request):
        data = await payload(request, MaintenanceConfirm)
        return await invoke(lambda: service.confirm(request_id, data, authorize=authorize))

    @app.delete("/api/maintenance/reviews/{request_id}")
    async def discard(request_id: str):
        return await invoke(lambda: service.discard(request_id))

    @app.get("/")
    async def index():
        return FileResponse(Path(assets) / "maintenance.html", media_type="text/html")

    @app.get("/trademind-mark.svg")
    async def brand():
        return FileResponse(Path(assets) / "trademind-mark.svg", media_type="image/svg+xml")

    app.mount("/assets", StaticFiles(directory=Path(assets) / "assets"), name="assets")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Private, separate directory for backups, restores and reviews",
    )
    parser.add_argument(
        "--frontend-dist", type=Path, default=Path(__file__).resolve().parents[3] / "frontend/dist"
    )
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if not (args.frontend_dist / "maintenance.html").is_file():
        parser.error("Build the frontend with npm run build before opening maintenance")
    service = MaintenanceConsole(args.data_dir, args.plugin_dir, args.workspace)
    token = secrets.token_urlsafe(32)
    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        app = create_console_app(service, token=token, origin=origin, assets=args.frontend_dist)
        # The one-session capability stays in the fragment, never in HTTP paths,
        # access logs or persistent frontend storage. Closing this process revokes it.
        print(
            f"Open the private maintenance link on this computer: {origin}/#key={token}", flush=True
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app, access_log=False, proxy_headers=False, server_header=False, log_level="warning"
            )
        )
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
