"""Shared current-session authorization for project-bound connector reads."""

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .constants import RunStatus
from .storage import FileLedger


def authorize_connector_run(
    ledger: FileLedger, access: ProjectAccessStore, project, run_id: str,
):
    run = ledger.get_run(run_id)
    if (
        run.project_id != project.id or not run.sdk_session_id
        or run.status != RunStatus.RUNNING
        or run.identity_binding != project.identity_binding
        or run.tenant_id != project.tenant_id
    ):
        raise AuthorizationError("Connector read requires this project's running SDK session")
    if run.identity_binding == "authenticated_principal":
        if not run.principal_id or run.principal_role is None or not run.tenant_id:
            raise AuthorizationError("Run has no bound principal")
        access.authorize_bound(
            project.id, tenant_id=run.tenant_id, principal_id=run.principal_id,
            role=run.principal_role, permission=ProjectPermission.TOOL_USE,
        )
    return run
