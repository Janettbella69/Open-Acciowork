"""The same durable device/project check applies to HTTP actions and push wakes."""

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .companion import CompanionError, PairingRecord
from .config import Settings
from .storage import FileLedger, LedgerNotFoundError


def authorize_companion_project(
    settings: Settings, ledger: FileLedger, access: ProjectAccessStore,
    record: PairingRecord, permission: ProjectPermission, project_id: str,
) -> None:
    if project_id not in record.project_ids:
        raise CompanionError("Project is outside device authorization")
    try:
        project = ledger.get_project(project_id)
        binding = record.principal_binding
        if binding is None:
            if settings.auth_mode != "local_only" or project.identity_binding != "local_only":
                raise AuthorizationError("device authority no longer matches deployment")
        else:
            if (
                settings.auth_mode != "trusted_gateway"
                or binding.tenant_id != settings.trusted_gateway_tenant_id
                or project.identity_binding != "authenticated_principal"
                or project.tenant_id != binding.tenant_id
            ):
                raise AuthorizationError("device is outside deployment tenant")
            access.authorize_bound(
                project_id, tenant_id=binding.tenant_id, principal_id=binding.principal_id,
                role=binding.role, permission=permission,
            )
    except (LedgerNotFoundError, AuthorizationError, ValueError) as exc:
        raise CompanionError("Device project access was revoked") from exc
