"""Human-only erasure review; authentication precedes request-lease handoff."""

import subprocess

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .conversation_erasure import ConversationErasure
from .data_operations import protected_call
from .deletion_models import ConversationDeletionRequest
from .erasure_files import DataOperationBusy, ErasureError
from .storage import LedgerNotFoundError


def register_conversation_erasure_routes(app, ledger, access, settings, runs, memory, read_body):
    service = ConversationErasure(ledger, access, settings, runs, memory)
    app.state.conversation_erasure = service

    async def invoke(operation, *, protect=True):
        try:
            if protect:
                return await run_in_threadpool(protected_call, ledger.root, operation)
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Conversation deletion requires the current owner or admin."
            ) from exc
        except LedgerNotFoundError as exc:
            raise HTTPException(404, "Conversation not found.") from exc
        except (ErasureError, DataOperationBusy) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                409,
                "Conversation deletion could not finish. "
                "Retry this request after recovery; do not assume it was cancelled.",
            ) from exc

    @app.get("/api/projects/{project_id}/conversations/{run_id}/deletion")
    async def preview(project_id: str, run_id: str, request: Request, response: Response):
        principal = getattr(request.state, "principal", None)
        response.headers["Cache-Control"] = "no-store"
        return await invoke(lambda: service.preview(project_id, principal, run_id=run_id))

    @app.post("/api/projects/{project_id}/conversations/{run_id}/deletion")
    async def delete(project_id: str, run_id: str, request: Request, response: Response):
        principal = getattr(request.state, "principal", None)
        await invoke(lambda: service.authorize(project_id, principal))
        raw = await read_body(
            request, max_bytes=2048, too_large_detail="Deletion request too large."
        )
        try:
            payload = ConversationDeletionRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid deletion confirmation.") from exc
        # Keep the authentication/source middleware intact. The worker owns its
        # exclusive lease through the transaction even if this request disconnects.
        request.state.data_operation_lease.release()
        response.headers["Cache-Control"] = "no-store"
        return await invoke(
            lambda: service.delete(project_id, run_id, principal, payload), protect=False
        )
