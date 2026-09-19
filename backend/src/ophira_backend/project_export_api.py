"""Project-scoped archive review and download for human owners/administrators."""

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .project_export_models import (
    ProjectExportConflict,
    ProjectExportPreview,
    ProjectExportRequest,
)
from .project_exports import ProjectExports
from .storage import LedgerNotFoundError
from .workspace_checkpoints import WorkspaceBusyError


def register_project_export_routes(app: FastAPI, ledger, access, settings, read_body):
    service = ProjectExports(ledger, access, settings)
    app.state.project_exports = service
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    async def result(project_id, request, operation):
        principal = getattr(request.state, "principal", None)
        try:
            service.authorize(project_id, principal)
            value = await run_in_threadpool(lambda: operation(principal))
            # A signed assertion can expire or membership can change during capture.
            service.authorize(project_id, principal)
            return value
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(403, "Project export access is unavailable", headers) from exc
        except (LedgerNotFoundError, FileNotFoundError) as exc:
            raise HTTPException(404, "Project export source not found", headers) from exc
        except ProjectExportConflict as exc:
            raise HTTPException(409, str(exc), headers) from exc
        except WorkspaceBusyError as exc:
            raise HTTPException(
                409, "Project files are busy. Try exporting again shortly.", headers,
            ) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "Project export could not be verified", headers) from exc

    @app.get("/api/projects/{project_id}/export", response_model=ProjectExportPreview)
    async def preview(project_id: str, request: Request, response: Response):
        response.headers.update(headers)
        return await result(
            project_id, request, lambda principal: service.preview(project_id, principal),
        )

    @app.post("/api/projects/{project_id}/export/download")
    async def download(project_id: str, request: Request):
        raw = await read_body(
            request, max_bytes=1024, too_large_detail="Project export request is too large",
        )
        try:
            payload = ProjectExportRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid project export request", headers) from exc
        archive = await result(
            project_id, request,
            lambda principal: service.download(project_id, principal, payload.fingerprint),
        )
        return Response(
            content=archive, media_type="application/zip",
            headers={
                **headers,
                "Content-Disposition": f'attachment; filename="{project_id}-export.zip"',
            },
        )
