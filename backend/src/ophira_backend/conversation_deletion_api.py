"""Conversation deletion uses opaque Run anchors; Agent tools cannot invoke it."""

from fastapi import HTTPException, Request

from .conversation_deletion import ConversationDeletionOperation, ConversationDeletionPreview
from .project_deletion_api import deletion_result
from .project_deletion_models import ProjectDeletionRequest


def register_conversation_deletion_routes(app, service, read_body):
    @app.get(
        "/api/projects/{project_id}/conversations/{run_id}/deletion",
        response_model=ConversationDeletionPreview,
    )
    async def preview(project_id: str, run_id: str, request: Request):
        return await deletion_result(
            lambda: service.preview(
                project_id,
                run_id,
                getattr(request.state, "principal", None),
            )
        )

    @app.post(
        "/api/projects/{project_id}/conversations/{run_id}/deletion",
        response_model=ConversationDeletionOperation,
    )
    async def start(project_id: str, run_id: str, request: Request):
        raw = await read_body(
            request, max_bytes=8192, too_large_detail="Conversation deletion request is too large."
        )
        try:
            payload = ProjectDeletionRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid conversation deletion request.") from exc
        return await deletion_result(
            lambda: service.start(
                project_id,
                run_id,
                payload,
                getattr(request.state, "principal", None),
            )
        )

    @app.get(
        "/api/conversation-deletions/{project_id}/{operation_id}",
        response_model=ConversationDeletionOperation,
    )
    async def status(project_id: str, operation_id: str, request: Request):
        return await deletion_result(
            lambda: service.get(
                project_id,
                operation_id,
                getattr(request.state, "principal", None),
            )
        )

    @app.post(
        "/api/conversation-deletions/{project_id}/{operation_id}/retry",
        response_model=ConversationDeletionOperation,
    )
    async def retry(project_id: str, operation_id: str, request: Request):
        return await deletion_result(
            lambda: service.retry(
                project_id,
                operation_id,
                getattr(request.state, "principal", None),
            )
        )
