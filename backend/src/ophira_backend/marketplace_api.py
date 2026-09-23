"""Administrator reads and human-owned project/shop authorization routes."""

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .connectors import ConnectorNotFoundError, ConnectorUnavailableError
from .constants import PrincipalRole
from .marketplace_access import (
    MarketplaceAccessService,
    MarketplaceGrantRequest,
    MarketplaceRevokeRequest,
)
from .marketplaces import (
    MARKETPLACE_GUIDES,
    MarketplaceError,
    MarketplaceReadRequest,
)
from .models import PrincipalContext
from .shein_authorization import (
    BeginSheinAuthorization,
    FinishSheinAuthorization,
    SheinAuthorizationService,
    SheinAuthorizationState,
)
from .storage import LedgerNotFoundError


def register_marketplace_routes(
    application: FastAPI, *,
    marketplace_access: MarketplaceAccessService,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
) -> None:
    reader = marketplace_access.reader
    application.state.marketplace_reader = reader
    application.state.marketplace_access = marketplace_access
    shein_authorization = SheinAuthorizationService(marketplace_access)
    application.state.shein_authorization = shein_authorization

    def authorization_principal(request, response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        principal = getattr(request.state, "principal", None)
        shein_authorization._actor(principal)
        return principal

    @application.post("/api/connectors/{connector_id}/shein-authorization/start")
    async def start_shein_authorization(
        connector_id: str, payload: BeginSheinAuthorization, request: Request, response: Response,
    ):
        principal = authorization_principal(request, response)
        try:
            return shein_authorization.begin(connector_id, payload, principal)
        except (ConnectorNotFoundError, ValueError):
            raise HTTPException(400, "Invalid SHEIN connector configuration") from None

    @application.post("/api/connectors/{connector_id}/shein-authorization/status")
    async def view_shein_authorization(
        connector_id: str, payload: SheinAuthorizationState, request: Request, response: Response,
    ):
        return shein_authorization.view(
            connector_id, payload, authorization_principal(request, response),
        )

    @application.post("/api/connectors/{connector_id}/shein-authorization/cancel")
    async def cancel_shein_authorization(
        connector_id: str, payload: SheinAuthorizationState, request: Request, response: Response,
    ):
        return shein_authorization.cancel(
            connector_id, payload, authorization_principal(request, response),
        )

    @application.post("/api/connectors/{connector_id}/shein-authorization/finish")
    async def finish_shein_authorization(
        connector_id: str, payload: FinishSheinAuthorization, request: Request, response: Response,
    ):
        principal = authorization_principal(request, response)
        try:
            return await shein_authorization.finish(connector_id, payload, principal)
        except (ConnectorNotFoundError, ConnectorUnavailableError):
            raise HTTPException(
                409, "SHEIN configuration changed. Start authorization again.",
            ) from None

    @application.exception_handler(MarketplaceError)
    async def marketplace_failure(_request: Request, exc: MarketplaceError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": f"{exc.code}: {exc}"},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(RequestValidationError)
    async def safe_connector_validation(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response contains input_value, including
        # an overlong/mistyped ConnectorUpsert.secret. Never echo credential input.
        if (
            request.url.path.startswith("/api/connectors/")
            or "/marketplace-access" in request.url.path
            or "/shops" in request.url.path
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "Invalid connector request. Check field types and limits."},
                headers={"Cache-Control": "no-store"},
            )
        return await request_validation_exception_handler(request, exc)

    @application.get("/api/connectors/marketplaces")
    async def marketplace_guides(response: Response) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return MARKETPLACE_GUIDES

    def authorize_project(request: Request, project_id: str, *, write: bool = False):
        principal = getattr(request.state, "principal", None)
        require_project_authorization(
            project_id, principal, ProjectPermission.PROJECT_ACCESS_MANAGE if write
            else ProjectPermission.PROJECT_READ,
        )
        return principal

    @application.get("/api/projects/{project_id}/marketplace-access")
    async def list_project_marketplaces(project_id: str, request: Request, response: Response):
        authorize_project(request, project_id)
        try:
            result = marketplace_access.view(project_id)
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project marketplace access is unavailable") from None
        authorize_project(request, project_id)
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/projects/{project_id}/marketplace-access")
    async def grant_project_marketplace(
        project_id: str, payload: MarketplaceGrantRequest, request: Request, response: Response,
    ):
        principal = authorize_project(request, project_id, write=True)
        try:
            result = await marketplace_access.grant(project_id, payload, principal)
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project administrator access is unavailable") from None
        except ConnectorUnavailableError as exc:
            raise HTTPException(409, f"{exc.code}: {exc}") from None
        authorize_project(request, project_id, write=True)
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/projects/{project_id}/marketplace-access/{binding_id}/revoke")
    async def revoke_project_marketplace(
        project_id: str, binding_id: str, payload: MarketplaceRevokeRequest,
        request: Request, response: Response,
    ):
        principal = authorize_project(request, project_id, write=True)
        try:
            result = marketplace_access.revoke(
                project_id, binding_id, payload.expected_revision, principal,
            )
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project administrator access is unavailable") from None
        authorize_project(request, project_id, write=True)
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/connectors/{connector_id}/marketplace-read")
    async def read_marketplace(
        connector_id: str, payload: MarketplaceReadRequest, request: Request, response: Response,
    ) -> dict:
        # Deliberate POST for a user-triggered credentialed read. Also enforce the
        # role here, independently of the shared connector administration guard.
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role not in {
            PrincipalRole.OWNER, PrincipalRole.ADMIN,
        }:
            raise HTTPException(status_code=403, detail="Tenant administrator required")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            result = await reader.read(connector_id, payload)
        except MarketplaceError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=f"{exc.code}: {exc}", headers=headers,
            ) from None
        except ConnectorUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail=f"{exc.code}: {exc}", headers=headers,
            ) from None
        response.headers.update(headers)
        return result
