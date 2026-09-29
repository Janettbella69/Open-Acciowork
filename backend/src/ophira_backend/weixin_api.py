"""Administrator pairing and explicit project binding for official Weixin text IM."""

from __future__ import annotations

import base64
from collections.abc import Callable

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from qrcode.exceptions import DataOverflowError

from .auth import AuthorizationError, ProjectPermission
from .constants import PrincipalRole
from .models import PrincipalContext, utc_now
from .policy import OrgPolicyStore
from .storage import LedgerNotFoundError
from .weixin_accounts import ACTIVE_PAIRING_STATES, WeixinPairing, WeixinStore, WeixinStoreError
from .weixin_transport import WeixinTransport, WeixinTransportError


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")
    enabled: bool
    expected_revision: int = Field(ge=1, strict=True)


class PollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verify_code: SecretStr | None = None


def install_weixin_routes(
    app: FastAPI, store: WeixinStore, transport: WeixinTransport, policy: OrgPolicyStore,
    principal_from_request: Callable[[Request], PrincipalContext | None],
    authorize_project: Callable[[str, PrincipalContext | None, ProjectPermission], None],
    identity_mode: str,
):
    def manageable(principal):
        return principal is None or (
            principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            and utc_now() < principal.assertion_expires_at
        )

    def actor(request, response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        principal = principal_from_request(request)
        if not manageable(principal):
            raise HTTPException(403, "Weixin management requires administrator access")
        return principal

    def public_account(account):
        return account.model_dump(mode="json", exclude={"principal_binding"})

    def pairing_view(pairing: WeixinPairing, principal):
        result = pairing.model_dump(mode="json", exclude={"principal_binding"})
        result["poll_after_ms"] = 1000
        if pairing.status in ACTIVE_PAIRING_STATES:
            content = store.pairing_secret(pairing.id, principal)["content"]
            # Encode the official string locally; no remote image service sees QR credentials.
            try:
                image = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathFillImage)
            except (DataOverflowError, ValueError):
                store.cancel_pairing(pairing.id, principal)
                raise HTTPException(
                    502, "Official QR payload exceeds local encoding limits",
                ) from None
            encoded = base64.b64encode(image.to_string()).decode("ascii")
            result["qr_svg_data_url"] = "data:image/svg+xml;base64," + encoded
        return result

    def failure(exc):
        if isinstance(exc, AuthorizationError):
            return HTTPException(403, "Weixin operation is outside the authorized scope")
        if isinstance(exc, (LedgerNotFoundError, KeyError)):
            return HTTPException(404, "Weixin resource not found")
        if isinstance(exc, WeixinStoreError):
            code = 404 if exc.code.endswith("NOT_FOUND") else 409
            return HTTPException(code, exc.code)
        if isinstance(exc, WeixinTransportError):
            return HTTPException(502, exc.code)
        return HTTPException(503, "Weixin operation could not be verified")

    @app.get("/api/channels/weixin")
    def overview(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        principal = principal_from_request(request)
        can_manage = manageable(principal)
        result = {"identity_mode": identity_mode, "can_manage": can_manage,
                  "available": True, "direct_text": True, "group_chat": False,
                  "delivery_enabled": policy.get().channel_delivery_enabled,
                  "accounts": [], "projects": []}
        if not can_manage:
            return result
        try:
            store.cleanup()
            for account in store.list_accounts():
                if account.tenant_id == (principal.tenant_id if principal else None):
                    result["accounts"].append(public_account(account))
            for project in store.ledger.list_projects():
                try:
                    authorize_project(project.id, principal, ProjectPermission.RUN_CREATE)
                except (HTTPException, AuthorizationError):
                    continue
                result["projects"].append({"id": project.id, "name": project.name})
            return result
        except (ValueError, OSError) as exc:
            raise failure(exc) from None

    @app.post("/api/channels/weixin/pairings", status_code=201)
    async def start(request: Request, response: Response):
        principal = actor(request, response)
        try:
            qr = await transport.start_login()
            actor(request, response)  # request assertion may expire during the network call
            pairing = store.create_pairing(qr.qrcode, qr.qrcode_img_content, principal)
            return pairing_view(pairing, principal)
        except (WeixinTransportError, ValueError, OSError, AuthorizationError) as exc:
            raise failure(exc) from None

    @app.post("/api/channels/weixin/pairings/{pairing_id}/poll")
    async def poll(pairing_id: str, payload: PollRequest, request: Request, response: Response):
        principal = actor(request, response)
        try:
            pairing = store.get_pairing(pairing_id, principal)
            if pairing.status not in ACTIVE_PAIRING_STATES:
                return pairing_view(pairing, principal)
            code = payload.verify_code.get_secret_value() if payload.verify_code else None
            if code is not None and (not code.isascii() or not code.isdigit() or len(code) > 12):
                raise HTTPException(400, "Invalid verification code")
            with store.poll_lease(pairing_id) as acquired:
                if not acquired:
                    raise HTTPException(409, "PAIRING_BUSY")
                secret = store.pairing_secret(pairing_id, principal)
                try:
                    status = await transport.login_status(secret["qrcode"], verify_code=code)
                except WeixinTransportError as exc:
                    if exc.code != "DESTINATION_REJECTED":
                        raise
                    status = None  # unsupported IDC; never retry or follow this destination
                actor(request, response)
                pairing = store.get_pairing(pairing_id, principal)
                if pairing.status not in ACTIVE_PAIRING_STATES:
                    return pairing_view(pairing, principal)
                if status is not None and status.status == "confirmed":
                    pairing = store.confirm_pairing(
                        pairing_id, principal, bot_id=status.ilink_bot_id,
                        owner_user_id=status.ilink_user_id, token=status.bot_token,
                        base_url=status.base_url,
                    )
                else:
                    state = ("unsupported_redirect" if status is None
                             or status.status == "scaned_but_redirect"
                             else status.status)
                    pairing = store.update_pairing(pairing_id, principal, state)
                return pairing_view(pairing, principal)
        except (WeixinTransportError, ValueError, OSError, AuthorizationError, KeyError) as exc:
            raise failure(exc) from None

    @app.delete("/api/channels/weixin/pairings/{pairing_id}")
    def cancel(pairing_id: str, request: Request, response: Response):
        principal = actor(request, response)
        try:
            pairing = store.cancel_pairing(pairing_id, principal)
            return pairing_view(pairing, principal)
        except (ValueError, OSError, AuthorizationError, KeyError) as exc:
            raise failure(exc) from None

    @app.put("/api/channels/weixin/accounts/{account_id}/binding")
    def bind(account_id: str, payload: BindingRequest, request: Request, response: Response):
        principal = actor(request, response)
        try:
            if payload.project_id is not None:
                authorize_project(payload.project_id, principal, ProjectPermission.RUN_CREATE)
            return public_account(store.bind(account_id, actor=principal, **payload.model_dump()))
        except (ValueError, OSError, AuthorizationError, LedgerNotFoundError) as exc:
            raise failure(exc) from None

    @app.delete("/api/channels/weixin/accounts/{account_id}")
    def revoke(account_id: str, expected_revision: int, request: Request, response: Response):
        principal = actor(request, response)
        try:
            return public_account(store.revoke(account_id, expected_revision, principal))
        except (ValueError, OSError, AuthorizationError) as exc:
            raise failure(exc) from None
