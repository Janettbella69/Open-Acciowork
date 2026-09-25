"""Human-reviewed workspace recovery; never exposed as an Agent tool."""

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import ProjectPermission
from .constants import TERMINAL_RUN_STATUSES
from .storage import LedgerNotFoundError
from .workspace_checkpoints import (
    CheckpointSummary,
    RestoreApplyRequest,
    RestorePreviewRequest,
    RetentionDeleteRequest,
    WorkspaceBusyError,
    WorkspaceCheckpoints,
    WorkspaceRecoveryError,
    WorkspaceRestoreView,
    WorkspaceRetentionView,
)


def register_workspace_recovery_routes(
    app: FastAPI, ledger, runs, authorize_project, read_body, local_operator_id: str,
):
    service = WorkspaceCheckpoints(ledger)
    app.state.workspace_checkpoints = service

    def authorization(request, project_id, *, write=False, manage=False):
        principal = getattr(request.state, "principal", None)

        def authorize():
            authorize_project(
                project_id, principal,
                (ProjectPermission.PROJECT_DELETE if manage else
                 ProjectPermission.PROJECT_WRITE if write else ProjectPermission.PROJECT_READ),
            )

        authorize()
        return principal, authorize

    def assert_idle(project_id):
        if any(run.status not in TERMINAL_RUN_STATUSES or runs.is_executing(run.id)
               for run in ledger.list_runs(project_id)):
            raise WorkspaceBusyError("Wait for all project runs to finish before recovering files.")

    async def result(response, authorize, operation):
        response.headers["Cache-Control"] = "no-store"
        try:
            value = await run_in_threadpool(operation)
            authorize()
            return value
        except (LedgerNotFoundError, FileNotFoundError) as exc:
            raise HTTPException(404, "Workspace recovery record not found") from exc
        except WorkspaceRecoveryError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(
                409, "Recovery could not be verified. Check the saved result before retrying.",
            ) from exc

    async def payload(request, model):
        raw = await read_body(
            request, max_bytes=1024, too_large_detail="Workspace recovery request is too large.",
        )
        try:
            return model.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid workspace recovery request.") from exc

    @app.get(
        "/api/projects/{project_id}/workspace/checkpoints", response_model=list[CheckpointSummary],
    )
    async def checkpoints(project_id: str, request: Request, response: Response):
        _, authorize = authorization(request, project_id)
        return await result(response, authorize, lambda: service.list(project_id))

    @app.post("/api/projects/{project_id}/workspace/restores", response_model=WorkspaceRestoreView)
    async def preview(project_id: str, request: Request, response: Response):
        principal, authorize = authorization(request, project_id, write=True)
        data = await payload(request, RestorePreviewRequest)
        return await result(response, authorize, lambda: WorkspaceRestoreView.of(service.preview(
            project_id, data.checkpoint_id,
            created_by=principal.principal_id if principal else local_operator_id,
            principal_session_id=principal.session_id if principal else None,
            authorize=authorize, assert_idle=lambda: assert_idle(project_id),
        )))

    @app.get(
        "/api/projects/{project_id}/workspace/restores/{restore_id}",
        response_model=WorkspaceRestoreView,
    )
    async def restored(project_id: str, restore_id: str, request: Request, response: Response):
        _, authorize = authorization(request, project_id)

        def read():
            with ledger._lock_for(project_id):
                authorize()
                service.recover(project_id)
                return WorkspaceRestoreView.of(service.get_restore(project_id, restore_id))

        return await result(response, authorize, read)

    @app.post(
        "/api/projects/{project_id}/workspace/restores/{restore_id}/apply",
        response_model=WorkspaceRestoreView,
    )
    async def apply(project_id: str, restore_id: str, request: Request, response: Response):
        principal, authorize = authorization(request, project_id, write=True)
        await payload(request, RestoreApplyRequest)
        return await result(response, authorize, lambda: WorkspaceRestoreView.of(service.apply(
            project_id, restore_id,
            created_by=principal.principal_id if principal else local_operator_id,
            principal_session_id=principal.session_id if principal else None,
            authorize=authorize, assert_idle=lambda: assert_idle(project_id),
        )))

    @app.get(
        "/api/projects/{project_id}/workspace/retention", response_model=WorkspaceRetentionView,
    )
    async def retention(project_id: str, request: Request, response: Response):
        _, authorize = authorization(request, project_id, manage=True)
        return await result(response, authorize, lambda: service.retention(
            project_id, authorize=authorize,
        ))

    @app.post(
        "/api/projects/{project_id}/workspace/retention/checkpoints/{checkpoint_id}/delete",
        response_model=WorkspaceRetentionView,
    )
    async def delete_checkpoint(
        project_id: str, checkpoint_id: str, request: Request, response: Response,
    ):
        _, authorize = authorization(request, project_id, manage=True)
        data = await payload(request, RetentionDeleteRequest)

        def operation():
            service.delete_checkpoint(
                project_id, checkpoint_id, expected_fingerprint=data.expected_fingerprint,
                authorize=authorize, assert_idle=lambda: assert_idle(project_id),
            )
            return service.retention(project_id, authorize=authorize)

        return await result(response, authorize, operation)

    @app.post(
        "/api/projects/{project_id}/workspace/retention/restores/{restore_id}/delete",
        response_model=WorkspaceRetentionView,
    )
    async def delete_expired_review(
        project_id: str, restore_id: str, request: Request, response: Response,
    ):
        _, authorize = authorization(request, project_id, manage=True)
        data = await payload(request, RetentionDeleteRequest)

        def operation():
            service.delete_expired_review(
                project_id, restore_id, expected_fingerprint=data.expected_fingerprint,
                authorize=authorize, assert_idle=lambda: assert_idle(project_id),
            )
            return service.retention(project_id, authorize=authorize)

        return await result(response, authorize, operation)

    @app.post(
        "/api/projects/{project_id}/workspace/retention/restores/{restore_id}/discard-copy",
        response_model=WorkspaceRetentionView,
    )
    async def discard_retained_copy(
        project_id: str, restore_id: str, request: Request, response: Response,
    ):
        _, authorize = authorization(request, project_id, manage=True)
        data = await payload(request, RetentionDeleteRequest)

        def operation():
            service.discard_retained_copy(
                project_id, restore_id, expected_fingerprint=data.expected_fingerprint,
                authorize=authorize, assert_idle=lambda: assert_idle(project_id),
            )
            return service.retention(project_id, authorize=authorize)

        return await result(response, authorize, operation)

    @app.post(
        "/api/projects/{project_id}/workspace/retention/restores/{restore_id}/archive",
        response_model=WorkspaceRetentionView,
    )
    async def archive_review(
        project_id: str, restore_id: str, request: Request, response: Response,
    ):
        principal, authorize = authorization(request, project_id, manage=True)
        data = await payload(request, RetentionDeleteRequest)

        def operation():
            service.archive_review(
                project_id, restore_id, expected_fingerprint=data.expected_fingerprint,
                archived_by=principal.principal_id if principal else local_operator_id,
                authorize=authorize, assert_idle=lambda: assert_idle(project_id),
            )
            return service.retention(project_id, authorize=authorize)

        return await result(response, authorize, operation)

    @app.post(
        "/api/projects/{project_id}/workspace/retention/snapshots/reclaim",
        response_model=WorkspaceRetentionView,
    )
    async def reclaim_snapshots(project_id: str, request: Request, response: Response):
        _, authorize = authorization(request, project_id, manage=True)
        data = await payload(request, RetentionDeleteRequest)

        def operation():
            service.reclaim_snapshots(
                project_id, expected_fingerprint=data.expected_fingerprint,
                authorize=authorize, assert_idle=lambda: assert_idle(project_id),
            )
            return service.retention(project_id, authorize=authorize)

        return await result(response, authorize, operation)
