"""Human-only project deletion, with operation reads independent of the project."""

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .auth import AuthorizationError
from .project_deletion import ProjectDeletionUncertainError
from .project_deletion_capture import ProjectDeletionConflict
from .project_deletion_models import (
    ProjectDeletionOperation,
    ProjectDeletionPreview,
    ProjectDeletionRequest,
)
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .storage import LedgerNotFoundError


async def deletion_result(operation):
    try:
        return await run_in_threadpool(operation)
    except AuthorizationError as exc:
        raise HTTPException(403, "Deletion operation access is unavailable.") from exc
    except ProjectDeletionUncertainError as exc:
        raise HTTPException(
            503, "Deletion may have started. Check the original operation before retrying.",
        ) from exc
    except (LedgerNotFoundError, ProjectUnavailableError, FileNotFoundError) as exc:
        raise HTTPException(404, "Project or deletion operation not found.") from exc
    except (ProjectDeletionConflict, ProjectLifecycleBusyError, OSError, ValueError) as exc:
        raise HTTPException(
            409, "Deletion could not be verified. Refresh the review or check this operation.",
        ) from exc


def register_project_deletion_routes(app, service, read_body):
    result = deletion_result

    @app.get("/api/projects/{project_id}/deletion", response_model=ProjectDeletionPreview)
    async def preview(project_id: str, request: Request):
        principal = getattr(request.state, "principal", None)
        return await result(lambda: service.preview(project_id, principal))

    @app.post("/api/projects/{project_id}/deletion", response_model=ProjectDeletionOperation)
    async def start(project_id: str, request: Request):
        raw = await read_body(
            request, max_bytes=8192, too_large_detail="Project deletion request is too large.",
        )
        try:
            payload = ProjectDeletionRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid project deletion request.") from exc
        principal = getattr(request.state, "principal", None)
        return await result(lambda: service.start(project_id, payload, principal))

    @app.get(
        "/api/project-deletions/{project_id}/{operation_id}",
        response_model=ProjectDeletionOperation,
    )
    async def status(project_id: str, operation_id: str, request: Request):
        principal = getattr(request.state, "principal", None)
        return await result(lambda: service.get(project_id, operation_id, principal))

    @app.post(
        "/api/project-deletions/{project_id}/{operation_id}/retry",
        response_model=ProjectDeletionOperation,
    )
    async def retry(project_id: str, operation_id: str, request: Request):
        principal = getattr(request.state, "principal", None)
        return await result(lambda: service.retry(project_id, operation_id, principal))
