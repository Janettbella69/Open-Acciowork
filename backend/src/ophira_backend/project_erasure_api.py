"""Human project deletion; the worker reauthorizes after the HTTP lease handoff."""

import subprocess

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .data_operations import protected_call
from .deletion_models import ProjectDeletionRequest
from .erasure_files import ErasureError
from .project_erasure import ProjectErasure
from .storage import LedgerNotFoundError


def register_project_erasure_routes(app, ledger, access, settings, runs, memory, read_body):
    service = ProjectErasure(ledger, access, settings, runs, memory)
    app.state.project_erasure = service

    async def invoke(operation, *, protect=True):
        try:
            if protect:
                return await run_in_threadpool(protected_call, ledger.root, operation)
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Project deletion requires the current owner or admin."
            ) from exc
        except LedgerNotFoundError as exc:
            raise HTTPException(404, "Project not found.") from exc
        except ErasureError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                409,
                "Project deletion could not finish. "
                "Retry this request after recovery; do not assume it was cancelled.",
            ) from exc

    @app.get("/api/projects/{project_id}/deletion")
    async def preview(project_id: str, request: Request, response: Response):
        principal = getattr(request.state, "principal", None)
        response.headers["Cache-Control"] = "no-store"
        return await invoke(lambda: service.preview(project_id, principal))

    @app.post("/api/projects/{project_id}/deletion")
    async def delete(project_id: str, request: Request, response: Response):
        principal = getattr(request.state, "principal", None)
        await invoke(lambda: service.authorize(project_id, principal, allow_deleted=True))
        raw = await read_body(
            request, max_bytes=2048, too_large_detail="Deletion request too large."
        )
        try:
            payload = ProjectDeletionRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid project deletion confirmation.") from exc
        request.state.data_operation_lease.release()
        response.headers["Cache-Control"] = "no-store"
        return await invoke(lambda: service.delete(project_id, principal, payload), protect=False)
