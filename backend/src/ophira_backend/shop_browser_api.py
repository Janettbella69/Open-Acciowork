"""Project-scoped shop configuration, browser controls and collected data."""

from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .connectors import ConnectorNotFoundError
from .shop_browser import ShopBrowserService
from .shop_browser_store import (
    ShopBrowserError,
    ShopGrantInput,
    ShopIdentityInput,
    ShopRevisionInput,
    ShopUpdateInput,
)


def register_shop_browser_routes(app: FastAPI, service: ShopBrowserService, authorize_project):
    app.state.shop_browsers = service

    @app.exception_handler(ShopBrowserError)
    async def error(_request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": f"{exc.code}: {exc}"},
            headers={"Cache-Control": "no-store"},
        )

    def check(request, project_id, *, admin=False):
        principal = getattr(request.state, "principal", None)
        authorize_project(
            project_id,
            principal,
            ProjectPermission.PROJECT_ACCESS_MANAGE if admin else ProjectPermission.PROJECT_READ,
        )
        return principal

    async def result(request, response, project_id, action, *, admin=False):
        principal = check(request, project_id, admin=admin)
        try:
            value = await action(principal)
        except (AuthenticationError, AuthorizationError):
            raise HTTPException(403, "Project shop access is unavailable") from None
        except ConnectorNotFoundError:
            raise HTTPException(400, "Platform connector not found") from None
        check(request, project_id, admin=admin)
        response.headers["Cache-Control"] = "no-store"
        return value

    @app.get("/api/projects/{project_id}/shops")
    async def list_shops(project_id: str, request: Request, response: Response):
        async def action(_principal):
            return service.list(project_id)

        return await result(request, response, project_id, action)

    @app.post("/api/projects/{project_id}/shops")
    async def create_shop(
        project_id: str, payload: ShopIdentityInput, request: Request, response: Response
    ):
        return await result(
            request,
            response,
            project_id,
            lambda principal: service.create(project_id, payload, principal),
            admin=True,
        )

    @app.put("/api/projects/{project_id}/shops/{shop_id}")
    async def update_shop(
        project_id: str,
        shop_id: str,
        payload: ShopUpdateInput,
        request: Request,
        response: Response,
    ):
        return await result(
            request,
            response,
            project_id,
            lambda principal: service.update(project_id, shop_id, payload, principal),
            admin=True,
        )

    @app.post("/api/projects/{project_id}/shops/{shop_id}/grant")
    async def grant_shop(
        project_id: str, shop_id: str, payload: ShopGrantInput, request: Request, response: Response
    ):
        return await result(
            request,
            response,
            project_id,
            lambda principal: service.grant(project_id, shop_id, payload, principal),
            admin=True,
        )

    @app.get("/api/projects/{project_id}/shops/{shop_id}/data")
    async def shop_data(project_id: str, shop_id: str, request: Request, response: Response):
        async def action(_principal):
            return service.data(project_id, shop_id)

        return await result(request, response, project_id, action)

    @app.post("/api/projects/{project_id}/shops/{shop_id}/{operation}")
    async def control_shop(
        project_id: str,
        shop_id: str,
        operation: Literal["open", "close", "revoke"],
        payload: ShopRevisionInput,
        request: Request,
        response: Response,
    ):
        async def action(principal):
            if operation == "revoke":
                return service.revoke(project_id, shop_id, payload.expected_revision, principal)
            method = service.open if operation == "open" else service.close
            return await method(project_id, shop_id, payload.expected_revision, principal)

        return await result(request, response, project_id, action, admin=True)
