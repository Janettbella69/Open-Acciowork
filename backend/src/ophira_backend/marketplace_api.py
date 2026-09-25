"""Administrator reads and human-owned project/shop authorization routes."""

import re
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .connectors import ConnectorUnavailableError
from .constants import PrincipalRole
from .marketplace_access import (
    MarketplaceAccessService,
    MarketplaceGrantRequest,
    MarketplaceRevokeRequest,
)
from .marketplaces import (
    MARKETPLACE_GUIDES,
    TIKTOK_OAUTH_CALLBACK_PATH,
    MarketplaceError,
    MarketplaceReadRequest,
    TikTokShopReader,
)
from .models import PrincipalContext
from .shopify import SHOPIFY_OAUTH_CALLBACK_PATH, ShopifyReader
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

    def shopify_reader(connector_id: str) -> ShopifyReader:
        try:
            adapter = reader.adapter(connector_id)
        except ConnectorUnavailableError as exc:
            raise HTTPException(404, f"{exc.code}: {exc}") from None
        if not isinstance(adapter, ShopifyReader):
            raise HTTPException(404, "Shopify connector not found")
        return adapter

    def tiktok_reader(connector_id: str) -> TikTokShopReader:
        try:
            adapter = reader.adapter(connector_id)
        except ConnectorUnavailableError as exc:
            raise HTTPException(404, f"{exc.code}: {exc}") from None
        if not isinstance(adapter, TikTokShopReader):
            raise HTTPException(404, "TikTok Shop connector not found")
        return adapter

    def require_tenant_admin(request: Request) -> None:
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role not in {
            PrincipalRole.OWNER, PrincipalRole.ADMIN,
        }:
            raise HTTPException(403, "Tenant administrator required")

    @application.get("/api/connectors/{connector_id}/shopify-oauth")
    async def shopify_oauth_status(connector_id: str, request: Request, response: Response):
        require_tenant_admin(request)
        response.headers["Cache-Control"] = "no-store"
        return shopify_reader(connector_id).oauth_status(
            connector_id, marketplace_access.settings.shopify_oauth_public_origin,
        )

    @application.post("/api/connectors/{connector_id}/shopify-oauth/start")
    async def start_shopify_oauth(connector_id: str, request: Request, response: Response):
        require_tenant_admin(request)
        settings = marketplace_access.settings
        if settings.auth_mode != "trusted_gateway" or settings.shopify_oauth_public_origin is None:
            raise HTTPException(409, "Configure OPHIRA_SHOPIFY_OAUTH_PUBLIC_ORIGIN first")
        result = shopify_reader(connector_id).begin_oauth(
            connector_id, settings.shopify_oauth_public_origin,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @application.get(SHOPIFY_OAUTH_CALLBACK_PATH)
    async def complete_shopify_oauth(connector_id: str, request: Request):
        await shopify_reader(connector_id).complete_oauth(
            connector_id, request.scope.get("query_string", b""),
        )
        return RedirectResponse(
            "/app/plugins/connectors?shopify=connected", status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @application.get("/api/connectors/{connector_id}/tiktok-shop-oauth")
    async def tiktok_oauth_status(connector_id: str, request: Request, response: Response):
        require_tenant_admin(request)
        response.headers["Cache-Control"] = "no-store"
        return tiktok_reader(connector_id).oauth_status(
            connector_id, marketplace_access.settings.tiktok_shop_oauth_public_origin,
        )

    @application.post("/api/connectors/{connector_id}/tiktok-shop-oauth/start")
    async def start_tiktok_oauth(connector_id: str, request: Request, response: Response):
        require_tenant_admin(request)
        settings = marketplace_access.settings
        if (
            settings.auth_mode != "trusted_gateway"
            or settings.tiktok_shop_oauth_public_origin is None
        ):
            raise HTTPException(409, "Configure OPHIRA_TIKTOK_SHOP_OAUTH_PUBLIC_ORIGIN first")
        result = tiktok_reader(connector_id).begin_oauth(
            connector_id, settings.tiktok_shop_oauth_public_origin,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @application.get(TIKTOK_OAUTH_CALLBACK_PATH)
    async def complete_tiktok_oauth(connector_id: str, request: Request):
        await tiktok_reader(connector_id).complete_oauth(
            connector_id, request.scope.get("query_string", b""),
        )
        return RedirectResponse(
            "/app/plugins/connectors?tiktok_shop=connected", status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @application.exception_handler(MarketplaceError)
    async def marketplace_failure(_request: Request, exc: MarketplaceError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": f"{exc.code}: {exc}"},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(RequestValidationError)
    async def safe_connector_validation(request: Request, exc: RequestValidationError):
        path = request.url.path
        if (
            request.method == "POST"
            and re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+/event-triggers", path)
        ) or (
            request.method == "PATCH"
            and re.fullmatch(
                r"/api/projects/[A-Za-z0-9_-]+/event-triggers/[A-Za-z0-9_-]+", path,
            )
        ):
            # A model-level validation error includes the complete request input.
            # Preserve field locations and messages without echoing the Mailgun key.
            return JSONResponse(status_code=422, content={"detail": [
                {key: item[key] for key in ("type", "loc", "msg") if key in item}
                for item in exc.errors()
            ]}, headers={"Cache-Control": "no-store"})
        # FastAPI's default validation response contains input_value, including
        # an overlong/mistyped ConnectorUpsert.secret. Never echo credential input.
        if (
            request.url.path.startswith("/api/connectors/")
            or "/marketplace-access" in request.url.path
            or "/gmail-access" in request.url.path
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
        require_tenant_admin(request)
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
