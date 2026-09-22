"""Current project authority for human collaboration services."""

from .auth import AuthorizationError, ProjectPermission
from .constants import PrincipalRole

LOCAL_OPERATOR_ID = "local-operator"


def project_actor(principal):
    return principal.principal_id if principal else LOCAL_OPERATOR_ID


def project_moderator(principal):
    return principal is None or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}


def authorize_project_scope(ledger, access, settings, project_id, principal, permission):
    project = ledger.get_project(project_id)
    if settings.auth_mode == "local_only":
        if principal is not None or project.identity_binding != "local_only":
            raise AuthorizationError("Project is outside local deployment scope")
    else:
        if (
            principal is None
            or project.identity_binding != "authenticated_principal"
            or project.tenant_id != settings.trusted_gateway_tenant_id
        ):
            raise AuthorizationError("Project is outside authenticated deployment scope")
        access.authorize(project_id, principal, permission or ProjectPermission.PROJECT_READ)
    return project
