"""Human project/session export review and private, one-shot ZIP responses."""

import asyncio

from fastapi import HTTPException, Query, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .auth import AuthenticationError, AuthorizationError
from .models import APIModel
from .project_exports import ProjectExportError, ProjectExports
from .storage import LedgerNotFoundError
from .workspace_checkpoints import WorkspaceBusyError


class ExportRequest(APIModel):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


class PrivateArchiveResponse(StreamingResponse):
    def __init__(self, archive, size, authorize):
        self.archive = archive

        async def chunks():
            while True:
                await run_in_threadpool(authorize)
                chunk = await run_in_threadpool(archive.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk

        super().__init__(
            chunks(),
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(size),
                "Content-Disposition": 'attachment; filename="ophira-project-data.zip"',
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            self.archive.close()


def register_project_export_routes(app, ledger, access, settings, runs, memory, read_body):
    service = ProjectExports(ledger, access, settings, runs, memory)
    app.state.project_exports = service
    slots = asyncio.Semaphore(1)

    async def invoke(operation):
        try:
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Project export requires the current project owner or admin."
            ) from exc
        except LedgerNotFoundError as exc:
            raise HTTPException(404, "Export project or conversation not found.") from exc
        except (ProjectExportError, WorkspaceBusyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(
                409, "Project export could not be verified. Refresh before retrying."
            ) from exc

    @app.get("/api/projects/{project_id}/data-export")
    async def preview(
        project_id: str,
        request: Request,
        response: Response,
        run_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$"),
    ):
        principal = getattr(request.state, "principal", None)
        await invoke(lambda: service.authorize(project_id, principal))
        response.headers["Cache-Control"] = "no-store"
        async with slots:
            return await invoke(lambda: service.preview(project_id, principal, run_id=run_id))

    @app.post("/api/projects/{project_id}/data-export")
    async def download(project_id: str, request: Request):
        principal = getattr(request.state, "principal", None)
        await invoke(lambda: service.authorize(project_id, principal))
        raw = await read_body(request, max_bytes=2048, too_large_detail="Export request too large.")
        try:
            payload = ExportRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid export request.") from exc
        async with slots:
            archive, size = await invoke(
                lambda: service.create(
                    project_id,
                    principal,
                    payload.fingerprint,
                    run_id=payload.run_id,
                )
            )
        return PrivateArchiveResponse(
            archive,
            size,
            lambda: service.authorize(project_id, principal),
        )
