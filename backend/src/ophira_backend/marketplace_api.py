"""Administrator-initiated marketplace reads; no Agent access or implicit imports."""

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .connectors import ConnectorRegistry, ConnectorUnavailableError
from .constants import PrincipalRole
from .marketplaces import (
    MARKETPLACE_GUIDES,
    MarketplaceError,
    MarketplaceReadRequest,
    TikTokShopReader,
)


def register_marketplace_routes(application: FastAPI, connectors: ConnectorRegistry) -> None:
    reader = TikTokShopReader(connectors)
    application.state.marketplace_reader = reader

    @application.exception_handler(RequestValidationError)
    async def safe_connector_validation(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response contains input_value, including
        # an overlong/mistyped ConnectorUpsert.secret. Never echo credential input.
        if request.url.path.startswith("/api/connectors/"):
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
