"""Human-owned SHEIN consent, one-attempt exchange and recoverable installation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .backup_files import parse_json
from .constants import PrincipalRole
from .marketplaces import MarketplaceError
from .models import Connector
from .secret_vault import CredentialChangedError
from .shein import (
    SHEIN_AUTH_PATH,
    SHEIN_DEFAULT_SCOPE,
    SHEIN_STORE_PATH,
    SHEIN_STORE_SCOPE,
    SheinCredentials,
    SheinReader,
    parse_shein_credentials,
)
from .skill_proposal_files import directory_fd, read_relative

CALLBACK_PREFIX = "/app/connectors/shein-authorize/"
STATE_PATTERN = r"^[a-f0-9]{32}\.[a-f0-9]{64}$"
FLOW_SECONDS = 30 * 60  # Platform tempToken separately expires ten minutes after consent.


def _error(
    message="The SHEIN authorization is unavailable. Start authorization again.", status=409
):
    return MarketplaceError("SHEIN_AUTHORIZATION_UNAVAILABLE", message, status)


def decrypt_shein_secret(ciphertext: str, app_secret: str) -> str:
    """Official AES-128-CBC/PKCS7 contract, with the documented fixed IV seed."""
    try:
        if not isinstance(ciphertext, str) or len(ciphertext) > 1024 or len(app_secret) < 16:
            raise ValueError
        encrypted = base64.b64decode(ciphertext, validate=True)
        if not encrypted or len(encrypted) % 16:
            raise ValueError
        decryptor = Cipher(
            algorithms.AES(app_secret.encode()[:16]),
            modes.CBC(b"space-station-default-iv"[:16]),
        ).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise _error("SHEIN returned an invalid encrypted seller key.", 502) from None


class BeginSheinAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    endpoint: Literal["https://openapi.sheincorp.com", "https://openapi.sheincorp.cn"]
    allow_products: bool
    redirect_origin: str = Field(min_length=1, max_length=500)


class SheinAuthorizationState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: SecretStr = Field(min_length=97, max_length=97)


class FinishSheinAuthorization(SheinAuthorizationState):
    app_id: str | None = Field(default=None, max_length=160)
    app_secret: SecretStr | None = Field(default=None, min_length=16, max_length=512)
    temp_token: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class SheinAuthorizationFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    app_id: str
    expected: Connector
    desired: Connector
    credential_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float
    created_at: float
    status: Literal["pending", "exchanging", "installing", "complete", "cancelled", "failed"]
    supplier_id: str | None = None


class SheinAuthorizationService:
    def __init__(self, marketplace_access, *, clock=time.time):
        self.access = marketplace_access
        self.connectors = marketplace_access.connectors
        self.settings = marketplace_access.settings
        self.ledger = marketplace_access.ledger
        self.root = self.ledger.root / "auth" / "shein-authorization"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with directory_fd(self.root):
            pass
        self.clock = clock

    def _actor(self, principal):
        if self.settings.auth_mode == "local_only":
            if principal is not None:
                raise _error(status=403)
            identity = ["local_only"]
        else:
            if (
                principal is None
                or principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
                or principal.tenant_id != self.settings.trusted_gateway_tenant_id
                or principal.assertion_expires_at.timestamp() <= self.clock()
            ):
                raise _error("A current tenant administrator session is required.", 403)
            identity = [
                "trusted_gateway",
                principal.tenant_id,
                principal.principal_id,
                principal.role.value,
                principal.session_id,
            ]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _save(self, flow):
        self.ledger._atomic_write(self.root / f"{flow.id}.json", flow)

    def _load(self, connector_id, state, principal, *, active=False):
        if not re.fullmatch(STATE_PATTERN, state):
            raise _error(status=400)
        identifier = state.split(".")[0]
        try:
            flow = SheinAuthorizationFlow.model_validate(
                parse_json(
                    read_relative(
                        self.root,
                        f"{identifier}.json",
                        16_000,
                    )
                )
            )
        except (OSError, ValueError):
            raise _error() from None
        if (
            flow.id != identifier
            or flow.expected.id != connector_id
            or not hmac.compare_digest(flow.state_hash, hashlib.sha256(state.encode()).hexdigest())
            or not hmac.compare_digest(flow.actor_hash, self._actor(principal))
        ):
            raise _error(status=403)
        if active and flow.expires_at <= self.clock():
            raise _error("This authorization expired. Start authorization again.")
        return flow

    def _current(self, flow, state, principal):
        current = self._load(flow.expected.id, state, principal, active=True)
        if current.status not in {"pending", "exchanging", "installing"}:
            raise _error()
        self.connectors.install_shein_authorization(
            flow.expected,
            flow.desired,
            flow.credential_revision,
            flow.id,
            authorize=lambda: self._actor(principal),
        )

    def begin(self, connector_id, payload: BeginSheinAuthorization, principal):
        actor = self._actor(principal)
        origin = payload.redirect_origin
        parts = urlsplit(origin)
        if (
            origin not in self.settings.cors_origins
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or not (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise _error("Configure this application's exact origin in OPHIRA_CORS_ORIGINS.", 400)
        with self.ledger._lock_for("shein-authorization-start"):
            paths = list(self.root.glob("*.json"))
            if len(paths) > 1000:
                raise _error("Too many authorization records. Contact the deployment operator.")
            for path in paths:
                try:
                    previous = SheinAuthorizationFlow.model_validate(
                        parse_json(
                            read_relative(
                                self.root,
                                path.name,
                                16_000,
                            )
                        )
                    )
                    if previous.expires_at < self.clock() - 30 * 86400:
                        path.unlink()
                    elif previous.expected.id == connector_id:
                        with self.ledger._lock_for(f"shein-authorization:{previous.id}"):
                            previous = SheinAuthorizationFlow.model_validate(
                                parse_json(
                                    read_relative(self.root, path.name, 16_000),
                                )
                            )
                            if previous.status in {"pending", "exchanging"}:
                                self._save(previous.model_copy(update={"status": "cancelled"}))
                except (OSError, ValueError):
                    raise _error("Authorization storage failed validation.") from None
            if len(list(self.root.glob("*.json"))) >= 1000:
                raise _error("Too many authorization records. Try again later.")
            with self.connectors.transaction():
                expected = self.connectors.get(connector_id)
                if expected.type != "shein" or self.connectors.vault is None:
                    raise _error(status=400)
                view = self.connectors.vault.describe(
                    connector_id,
                    fallback=self.connectors.environment_credential(expected),
                )
                desired = expected.model_copy(
                    update={
                        "endpoint": payload.endpoint,
                        "scope": SHEIN_DEFAULT_SCOPE
                        if payload.allow_products
                        else SHEIN_STORE_SCOPE,
                        "enabled": True,
                        "health": "unknown",
                        "last_checked_at": None,
                    }
                )
                identifier = uuid.uuid4().hex
                state = identifier + "." + secrets.token_hex(32)
                flow = SheinAuthorizationFlow(
                    id=identifier,
                    state_hash=hashlib.sha256(state.encode()).hexdigest(),
                    actor_hash=actor,
                    app_id=payload.app_id,
                    expected=expected,
                    desired=desired,
                    credential_revision=view["revision"],
                    created_at=self.clock(),
                    expires_at=self.clock() + FLOW_SECONDS,
                    status="pending",
                )
                self._save(flow)
        callback = origin + CALLBACK_PREFIX + connector_id
        url = "https://openapi-sem.sheincorp.com/#/empower?" + urlencode(
            {
                "appid": flow.app_id,
                "redirectUrl": base64.b64encode(callback.encode()).decode(),
                "state": state,
            }
        )
        return {"authorization_url": url, "expires_at": flow.expires_at}

    def view(self, connector_id, payload: SheinAuthorizationState, principal):
        state = payload.state.get_secret_value()
        flow = self._load(connector_id, state, principal)
        return {
            "app_id": flow.app_id,
            "endpoint": flow.desired.endpoint,
            "scope": flow.desired.scope,
            "supplier_id": flow.supplier_id,
            "expires_at": flow.expires_at,
            "status": "expired"
            if flow.expires_at <= self.clock() and flow.status != "complete"
            else flow.status,
        }

    def cancel(self, connector_id, payload: SheinAuthorizationState, principal):
        state = payload.state.get_secret_value()
        flow = self._load(connector_id, state, principal)
        with self.ledger._lock_for(f"shein-authorization:{flow.id}"):
            flow = self._load(connector_id, state, principal)
            if flow.status == "complete":
                raise _error("The connection was saved. Revoke it from Connectors.")
            # An installation may already have committed before its response was lost.
            if flow.status == "installing":
                raise _error("Check the saved connection before revoking it from Connectors.")
            self._save(flow.model_copy(update={"status": "cancelled"}))
        return {"status": "cancelled"}

    async def finish(self, connector_id, payload: FinishSheinAuthorization, principal):
        state = payload.state.get_secret_value()
        flow = self._load(connector_id, state, principal, active=True)
        try:
            with self.ledger._lock_for(f"shein-authorization:{flow.id}"):
                flow = self._load(connector_id, state, principal, active=True)
                if flow.status in {"installing", "complete"}:
                    installed = self.connectors.install_shein_authorization(
                        flow.expected,
                        flow.desired,
                        flow.credential_revision,
                        flow.id,
                        authorize=lambda: self._actor(principal),
                    )
                    if installed is not None:
                        self._save(flow.model_copy(update={"status": "complete"}))
                        return installed
                if flow.status != "pending":
                    raise _error(
                        "The previous exchange may have reached SHEIN. Start a new authorization."
                    )
                if (
                    payload.app_id != flow.app_id
                    or payload.app_secret is None
                    or payload.temp_token is None
                ):
                    raise _error("Enter the app secret for this authorization.", 400)
                secret, token = (
                    payload.app_secret.get_secret_value(),
                    payload.temp_token.get_secret_value(),
                )
                if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]{16,512}", secret) or not re.fullmatch(
                    r"[A-Za-z0-9_.~:+/=-]{1,4096}", token
                ):
                    raise _error("Invalid app credentials or temporary token.", 400)
                self._current(flow, state, principal)
                flow = flow.model_copy(update={"status": "exchanging"})
                self._save(flow)  # Durable before any possibly single-use remote exchange.
            adapter = self.access.reader.adapter(connector_id)
            reader = SheinReader(
                self.connectors, transport_factory=adapter.transport_factory, clock=self.clock
            )
            private = {flow.app_id, secret, token, state}

            def current():
                self._current(flow, state, principal)

            async with asyncio.timeout(30):
                response = await reader._request(
                    flow.desired.endpoint,
                    SHEIN_AUTH_PATH,
                    {"tempToken": token},
                    SheinCredentials(open_key_id=flow.app_id, secret_key=secret),
                    private,
                    current,
                )
                info = response["info"]
                supplier = info.get("supplierId")
                if (
                    info.get("appid") != flow.app_id
                    or info.get("state") != state
                    or type(supplier) is not int
                    or not 0 < supplier < 2**63
                ):
                    raise _error("SHEIN returned a mismatched app, state or seller identity.", 502)
                seller_secret = decrypt_shein_secret(info.get("secretKey"), secret)
                raw = json.dumps(
                    {
                        "open_key_id": info.get("openKeyId"),
                        "secret_key": seller_secret,
                        "authorization_id": flow.id,
                        "bound_endpoint": flow.desired.endpoint,
                        "bound_scope": flow.desired.scope,
                        "supplier_id": str(supplier),
                    }
                )
                try:
                    credentials = parse_shein_credentials(raw)
                except ValueError:
                    raise _error("SHEIN returned invalid seller credentials.", 502) from None
                private.update({seller_secret, credentials.open_key_id.get_secret_value()})
                profile = await reader._request(
                    flow.desired.endpoint, SHEIN_STORE_PATH, {}, credentials, private, current
                )
                store = profile["info"].get("storeInfo")
                if (
                    not isinstance(store, dict)
                    or type(store.get("supplierId")) is not int
                    or store["supplierId"] != supplier
                ):
                    raise _error("The exchanged key does not match the authorized seller.", 502)
                current()
            with self.ledger._lock_for(f"shein-authorization:{flow.id}"):
                current()
                flow = flow.model_copy(
                    update={"status": "installing", "supplier_id": str(supplier)}
                )
                self._save(flow)
                installed = self.connectors.install_shein_authorization(
                    flow.expected,
                    flow.desired,
                    flow.credential_revision,
                    flow.id,
                    raw=raw,
                    authorize=lambda: self._actor(principal),
                )
                self._save(flow.model_copy(update={"status": "complete"}))
                return installed
        except CredentialChangedError:
            raise _error(
                "Connector settings or credentials changed. Start authorization again."
            ) from None
        except (TimeoutError, httpx.HTTPError, OSError):
            raise _error(
                "The exchange was interrupted. Check its status before starting again.", 502
            ) from None
