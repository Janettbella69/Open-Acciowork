"""Project discussions for authenticated members or the local operator."""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .project_comments import (
    CollaborationConflict,
    CollaborationUnavailable,
    CommentChange,
    CommentCreate,
    ProjectComments,
)
from .storage import EventReplayLimitError, LedgerNotFoundError


def register_collaboration_routes(app: FastAPI, ledger, access, settings, read_body):
    comments = ProjectComments(ledger, access, settings)
    app.state.project_comments = comments
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    def principal(request):
        return getattr(request.state, "principal", None)

    async def result(response, operation):
        response.headers.update(headers)
        try:
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Project collaboration access is unavailable", headers
            ) from exc
        except LedgerNotFoundError as exc:
            raise HTTPException(404, "Discussion or project target not found", headers) from exc
        except CollaborationConflict as exc:
            raise HTTPException(409, str(exc), headers) from exc
        except (CollaborationUnavailable, EventReplayLimitError, OSError) as exc:
            raise HTTPException(409, "Comment history could not be verified", headers) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid discussion or comment target", headers) from exc

    async def payload(request, model):
        raw = await read_body(
            request, max_bytes=32 * 1024, too_large_detail="Comment request is too large"
        )
        try:
            return model.model_validate_json(raw)
        except ValueError as exc:
            # Validation errors must not echo a pasted credential or an old draft.
            raise HTTPException(422, "Invalid comment request", headers) from exc

    @app.get("/api/projects/{project_id}/collaboration")
    async def capabilities(project_id: str, request: Request, response: Response):
        return await result(response, lambda: comments.capabilities(project_id, principal(request)))

    @app.get("/api/projects/{project_id}/comments")
    async def discussions(
        project_id: str,
        request: Request,
        response: Response,
        before: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        return await result(
            response,
            lambda: comments.list(
                project_id,
                principal(request),
                before=before,
                limit=limit,
            ),
        )

    @app.post("/api/projects/{project_id}/comments")
    async def create(project_id: str, request: Request, response: Response):
        data = await payload(request, CommentCreate)
        return await result(response, lambda: comments.create(project_id, data, principal(request)))

    @app.get("/api/projects/{project_id}/comments/{comment_id}/thread")
    async def thread(
        project_id: str,
        comment_id: str,
        request: Request,
        response: Response,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        return await result(
            response,
            lambda: comments.thread(
                project_id,
                comment_id,
                principal(request),
                after=after,
                limit=limit,
            ),
        )

    @app.patch("/api/projects/{project_id}/comments/{comment_id}")
    async def change(project_id: str, comment_id: str, request: Request, response: Response):
        data = await payload(request, CommentChange)
        return await result(
            response,
            lambda: comments.change(
                project_id,
                comment_id,
                data,
                principal(request),
            ),
        )

    @app.get("/api/projects/{project_id}/comments/{comment_id}/history")
    async def history(project_id: str, comment_id: str, request: Request, response: Response):
        return await result(
            response,
            lambda: comments.history(
                project_id,
                comment_id,
                principal(request),
            ),
        )
