"""Official marketplace contracts and a bounded, seller-authorized TikTok Shop reader.

Only the documented read operations below are callable. Credential material,
shop ciphers, signed URLs and upstream error bodies never leave this adapter.
The Shopify reader in ``shopify.py`` shares the contracts and transport bounds here.
See docs/marketplace-connectors.md for source versions and acceptance limits.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import utc_now
from .untrusted import UntrustedSource, sanitize_untrusted_text
from .web_tools import pin_public_http_url

if TYPE_CHECKING:
    from .connectors import CommercialConnectorRuntime, ConnectorRegistry
    from .web_tools import PinnedPublicDestination

TIKTOK_SHOP_TYPE = "tiktok_shop"
SHOPIFY_TYPE = "shopify"
TIKTOK_SHOP_ORIGIN = "https://open-api.tiktokglobalshop.com"
TIKTOK_SHOPS_PATH = "/authorization/202309/shops"
TIKTOK_PRODUCTS_PATH = "/product/202502/products/search"
TIKTOK_AUTH_SCOPE = "seller.authorization.info"
TIKTOK_PRODUCT_SCOPE = "seller.product.basic"
TIKTOK_OAUTH_ORIGIN = "https://auth.tiktok-shops.com"
TIKTOK_TOKEN_PATH = "/api/v2/token/get"
TIKTOK_REFRESH_PATH = "/api/v2/token/refresh"
TIKTOK_OAUTH_CALLBACK_PATH = "/api/connectors/{connector_id}/tiktok-shop-oauth/callback"
TIKTOK_OAUTH_CALLBACK_RE = re.compile(
    TIKTOK_OAUTH_CALLBACK_PATH.replace("{connector_id}", r"[A-Za-z0-9_-]+")
)
_TIKTOK_PENDING_PREFIX = "tiktok-shop-oauth-pending:"
_TIKTOK_TOKEN_PREFIX = "tiktok-shop-oauth-token:"
_TIKTOK_STATE_SECONDS = 600
_TIKTOK_REFRESH_MARGIN = 60
MAX_MARKETPLACE_RESPONSE_BYTES = 2 * 1024 * 1024

MARKETPLACE_GUIDES = [
    {
        "type": "shein", "name": "SHEIN", "read_adapter": False,
        "documentation_url": "https://open.sheincorp.com/documents/system/"
        "dad0d1c7-be76-4b03-a735-4e23f012bdd9",
    },
    {
        "type": "temu", "name": "Temu", "read_adapter": False,
        "documentation_url": "https://partner.temu.com/documentation?"
        "menu_code=52ef88bdef1d4527b15f6d303b173e48",
    },
    {
        "type": TIKTOK_SHOP_TYPE, "name": "TikTok Shop", "read_adapter": True,
        "documentation_url": "https://partner.tiktokshop.com/docv2/page/developer-onboarding",
    },
    {
        "type": "aliexpress", "name": "AliExpress", "read_adapter": False,
        "documentation_url": "https://developer.alibaba.com/docs/doc.htm?"
        "articleId=120674&docType=1&treeId=727",
    },
    {
        "type": "shopee", "name": "Shopee", "read_adapter": False,
        "documentation_url": "https://open.shopee.cn/developer-guide/12",
    },
    {
        "type": SHOPIFY_TYPE, "name": "Shopify", "read_adapter": True,
        "documentation_url": "https://shopify.dev/docs/apps/build/authentication-authorization",
    },
]


class MarketplaceError(RuntimeError):
    """Deliberately fixed, credential-free errors at the adapter boundary."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TikTokShopCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    app_key: SecretStr = Field(min_length=1, max_length=160)
    app_secret: SecretStr = Field(min_length=1, max_length=512)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    # TikTok's *_expire_in token response fields contain an absolute Unix timestamp.
    access_token_expires_at: int = Field(gt=0, lt=100_000_000_000)
    user_type: Literal[0] = 0

    @field_validator("user_type", mode="before")
    @classmethod
    def seller_principal_type(cls, value: Any) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("seller principal required")
        return value

    @model_validator(mode="after")
    def safe_header_values(self) -> TikTokShopCredentials:
        for value in (self.app_key, self.app_secret, self.access_token):
            if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", value.get_secret_value()):
                raise ValueError("invalid credential encoding")
        return self


def check_tiktok_seller_authorization_url(value: str) -> bool:
    """Accept only the seller authorization link copied from Partner Center."""
    if len(value) > 500 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        return False
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, strict_parsing=True, max_num_fields=2)
    except ValueError:
        return False
    host = parts.hostname or ""
    return bool(
        parts.scheme == "https"
        and parts.netloc == host
        and re.fullmatch(r"services(?:\.[a-z]{2,8})?\.tiktokshop\.com", host)
        and parts.path == "/open/authorize"
        and not parts.fragment
        and len(pairs) == 1
        and pairs[0][0] == "service_id"
        and re.fullmatch(r"[0-9]{1,40}", pairs[0][1])
    )


class TikTokShopOAuthCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    auth_mode: Literal["seller_oauth"]
    app_key: SecretStr = Field(min_length=1, max_length=160)
    app_secret: SecretStr = Field(min_length=1, max_length=512)
    authorization_url: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_seller_app(self) -> TikTokShopOAuthCredentials:
        if not check_tiktok_seller_authorization_url(self.authorization_url):
            raise ValueError("invalid seller authorization link")
        for value in (self.app_key, self.app_secret):
            if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", value.get_secret_value()):
                raise ValueError("invalid credential encoding")
        return self


def parse_tiktok_shop_credentials(
    raw: str,
) -> TikTokShopCredentials | TikTokShopOAuthCredentials:
    try:
        if len(raw) > 8000:
            raise ValueError("too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid")
        if payload.get("auth_mode") == "seller_oauth":
            return TikTokShopOAuthCredentials.model_validate(payload)
        return TikTokShopCredentials.model_validate(payload)
    except (ValidationError, ValueError, TypeError, RecursionError):
        # Pydantic errors include input_value, so never relay their text.
        raise ValueError("TikTok Shop requires a valid seller credential bundle") from None


def tiktok_oauth_vault_keys(connector_id: str) -> tuple[str, str]:
    return _TIKTOK_PENDING_PREFIX + connector_id, _TIKTOK_TOKEN_PREFIX + connector_id


ProductStatus = Literal[
    "ALL", "DRAFT", "PENDING", "FAILED", "ACTIVATE", "SELLER_DEACTIVATED",
    "PLATFORM_DEACTIVATED", "FREEZE", "DELETED", "ACTIVE", "ARCHIVED", "UNLISTED",
]
# Each platform documents its own status vocabulary; filters never cross platforms.
TIKTOK_PRODUCT_STATUSES = frozenset({
    "ALL", "DRAFT", "PENDING", "FAILED", "ACTIVATE", "SELLER_DEACTIVATED",
    "PLATFORM_DEACTIVATED", "FREEZE", "DELETED",
})
SHOPIFY_PRODUCT_STATUSES = frozenset({"ALL", "ACTIVE", "ARCHIVED", "DRAFT", "UNLISTED"})


class MarketplaceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["authorized_shops", "products", "orders"]
    shop_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    page_size: int = Field(default=20, ge=1, le=100)
    page_token: str | None = Field(default=None, min_length=1, max_length=2048)
    product_status: ProductStatus = "ALL"

    @model_validator(mode="after")
    def require_shop(self) -> MarketplaceReadRequest:
        if self.operation in {"products", "orders"} and not self.shop_id:
            raise ValueError("Select an authorized shop before reading products or orders")
        if self.operation == "orders" and self.product_status != "ALL":
            raise ValueError("Order reads do not accept product filters")
        if self.operation == "authorized_shops" and (
            self.shop_id is not None or self.page_token is not None
            or self.product_status != "ALL" or self.page_size != 20
        ):
            raise ValueError("Shop listing does not accept product filters")
        return self


def sign_tiktok_shop_request(
    path: str, parameters: dict[str, str], body: bytes, app_secret: str,
) -> str:
    """Sign the exact transmitted UTF-8 body and sorted, unencoded query values."""
    query = "".join(
        key + parameters[key] for key in sorted(parameters) if key not in {"sign", "access_token"}
    )
    secret = app_secret.encode("utf-8")
    message = secret + path.encode("utf-8") + query.encode("utf-8") + body + secret
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _protocol_error() -> MarketplaceError:
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID", "The marketplace returned an invalid response.", 502,
    )


def default_marketplace_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(
        trust_env=False, retries=0, limits=httpx.Limits(max_keepalive_connections=0),
    )


async def pin_marketplace_destination(url: str) -> PinnedPublicDestination:
    """Resolve one adapter-owned URL to a validated public unicast address."""
    from .connectors import is_public_unicast_address

    # Pin only the adapter's origin. The caller/model cannot supply a host or route.
    destination, _reason = await asyncio.to_thread(pin_public_http_url, url)
    if destination is None or not is_public_unicast_address(
        ipaddress.ip_address(destination.resolved_ip)
    ):
        raise MarketplaceError(
            "MARKETPLACE_DESTINATION_REJECTED", "The marketplace host could not be validated.",
            502,
        )
    return destination


async def send_marketplace_request(
    transport_factory: Callable[[], httpx.AsyncBaseTransport], request: httpx.Request,
    reject: Callable[[int], MarketplaceError],
) -> tuple[bytes, httpx.Headers]:
    """Send once and read at most the response cap; rejected bodies are never read."""
    # Public transport API avoids AsyncClient's INFO log of the signed URL.
    # It has no proxy, cookies, redirect handling, retries or second DNS lookup.
    async with transport_factory() as transport:
        response = await transport.handle_async_request(request)
        try:
            if response.status_code != 200:
                raise reject(response.status_code)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise _protocol_error()
            content = bytearray()
            async for chunk in response.aiter_raw():
                if len(content) + len(chunk) > MAX_MARKETPLACE_RESPONSE_BYTES:
                    raise MarketplaceError(
                        "MARKETPLACE_RESPONSE_TOO_LARGE",
                        "The marketplace response exceeds the read limit.", 502,
                    )
                content.extend(chunk)
        finally:
            await response.aclose()
    return bytes(content), response.headers


class TikTokShopReader:
    def __init__(
        self, connectors: ConnectorRegistry, *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock

    @asynccontextmanager
    async def _oauth_lock(self, connector_id: str):
        safe_id = self.connectors._safe_id(connector_id)
        descriptor = os.open(
            self.connectors.root / f".tiktok-shop-oauth-{safe_id}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        acquired = False
        try:
            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _oauth_runtime(
        self, connector_id: str,
    ) -> tuple[CommercialConnectorRuntime, TikTokShopOAuthCredentials]:
        runtime = self.connectors.require_tiktok_shop_runtime(connector_id)
        credentials = parse_tiktok_shop_credentials(runtime.credential.get_secret_value())
        if not isinstance(credentials, TikTokShopOAuthCredentials):
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_NOT_CONFIGURED", "Configure seller authorization first.",
            )
        if self.connectors.vault is None:
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_NOT_CONFIGURED", "Connector vault is unavailable.",
            )
        return runtime, credentials

    def oauth_status(self, connector_id: str, public_origin: str | None) -> dict[str, Any]:
        connector_id = self.connectors._safe_id(connector_id)
        callback_url = (
            public_origin + TIKTOK_OAUTH_CALLBACK_PATH.format(connector_id=connector_id)
            if public_origin else None
        )
        try:
            runtime, _credentials = self._oauth_runtime(connector_id)
            token = self._stored_oauth_token(connector_id, runtime)
            authorized = (
                token["refresh_expires_at"] > self.clock() + _TIKTOK_REFRESH_MARGIN
                and set(runtime.connector.scope.split()).issubset(token["scopes"])
            )
            mode = "seller_oauth"
        except RuntimeError:
            try:
                runtime = self.connectors.require_tiktok_shop_runtime(connector_id)
                mode = (
                    "seller_oauth" if isinstance(
                        parse_tiktok_shop_credentials(runtime.credential.get_secret_value()),
                        TikTokShopOAuthCredentials,
                    ) else "manual_token"
                )
            except (RuntimeError, ValueError):
                mode = "manual_token"
            authorized = False
        return {"auth_mode": mode, "authorized": authorized, "callback_url": callback_url}

    def begin_oauth(self, connector_id: str, public_origin: str) -> dict[str, str]:
        connector_id = self.connectors._safe_id(connector_id)
        runtime, credentials = self._oauth_runtime(connector_id)
        if TIKTOK_AUTH_SCOPE not in runtime.connector.scope.split():
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Configure seller shop read permission first.",
            )
        state = secrets.token_urlsafe(32)
        pending_key, _ = tiktok_oauth_vault_keys(connector_id)
        with self.connectors._transaction():
            if self.connectors.require_tiktok_shop_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED", "TikTok Shop connector changed.",
                )
            self.connectors.vault.set(pending_key, json.dumps({
                "state_hash": hashlib.sha256(state.encode("ascii")).hexdigest(),
                "revision": runtime.connector.revision,
                "expires_at": self.clock() + _TIKTOK_STATE_SECONDS,
            }))
        separator = "&" if "?" in credentials.authorization_url else "?"
        return {
            "authorization_url": (
                credentials.authorization_url + separator + urlencode({"state": state})
            ),
            "callback_url": public_origin + TIKTOK_OAUTH_CALLBACK_PATH.format(
                connector_id=connector_id,
            ),
        }

    async def complete_oauth(self, connector_id: str, raw_query: bytes) -> None:
        connector_id = self.connectors._safe_id(connector_id)
        runtime, credentials = self._oauth_runtime(connector_id)
        try:
            if len(raw_query) > 16_000:
                raise ValueError
            pairs = parse_qsl(
                raw_query.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                max_num_fields=20, encoding="utf-8", errors="strict",
            )
            values = dict(pairs)
            state = values.get("state", "")
            if (
                len(values) != len(pairs) or not 32 <= len(state) <= 128
                or not state.isascii()
                or ("app_key" in values
                    and values["app_key"] != credentials.app_key.get_secret_value())
            ):
                raise ValueError
        except (UnicodeError, ValueError):
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_INVALID", "Invalid TikTok Shop authorization callback.", 403,
            ) from None
        pending_key, token_key = tiktok_oauth_vault_keys(connector_id)
        with self.connectors._transaction():
            raw_pending = self.connectors.vault.get(pending_key)
            try:
                pending = json.loads(raw_pending or "")
                valid = (
                    isinstance(pending, dict)
                    and pending.get("revision") == runtime.connector.revision
                    and type(pending.get("expires_at")) in (int, float)
                    and pending["expires_at"] > self.clock()
                    and hmac.compare_digest(
                        str(pending.get("state_hash", "")),
                        hashlib.sha256(state.encode("ascii")).hexdigest(),
                    )
                    and self.connectors.require_tiktok_shop_runtime(connector_id) == runtime
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise MarketplaceError(
                    "MARKETPLACE_OAUTH_INVALID", "TikTok Shop authorization expired or changed.",
                    403,
                )
            self.connectors.vault.delete(pending_key)
        if values.get("error"):
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_DENIED", "TikTok Shop authorization was declined.", 403,
            )
        code = values.get("code", "")
        if not 1 <= len(code) <= 8_000 or any(ord(char) < 33 for char in code):
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_INVALID", "Invalid TikTok Shop authorization code.", 403,
            )

        def check_current() -> None:
            if self.connectors.require_tiktok_shop_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED", "TikTok Shop connector changed.",
                )

        async with self._oauth_lock(connector_id):
            record = await self._exchange_oauth_token(
                TIKTOK_TOKEN_PATH, credentials, "auth_code", code, check_current,
            )
            if not set(runtime.connector.scope.split()).issubset(record["scopes"]):
                raise MarketplaceError(
                    "MARKETPLACE_SCOPE_REQUIRED",
                    "TikTok Shop did not grant the configured seller read permissions.", 403,
                )
            with self.connectors._transaction():
                check_current()
                if self.connectors.vault.has(pending_key):
                    raise MarketplaceError(
                        "MARKETPLACE_CONFIGURATION_CHANGED", "Seller authorization restarted.",
                    )
                self.connectors.vault.set(token_key, json.dumps({
                    **record, "scopes": sorted(record["scopes"]),
                    "revision": runtime.connector.revision,
                }))

    def _stored_oauth_token(
        self, connector_id: str, runtime: CommercialConnectorRuntime,
    ) -> dict[str, Any]:
        assert self.connectors.vault is not None
        _, token_key = tiktok_oauth_vault_keys(connector_id)
        raw = self.connectors.vault.get(token_key)
        try:
            record = json.loads(raw or "")
            if (
                not isinstance(record, dict)
                or record.get("revision") != runtime.connector.revision
                or not all(
                    isinstance(record.get(name), str)
                    and 1 <= len(record[name]) <= 4096
                    and re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", record[name])
                    for name in ("access_token", "refresh_token", "open_id")
                )
                or any(
                    type(record.get(name)) is not int
                    or not 0 < record[name] < 100_000_000_000
                    for name in ("expires_at", "refresh_expires_at")
                )
                or not isinstance(record.get("scopes"), list)
                or not 1 <= len(record["scopes"]) <= 200
                or not all(
                    isinstance(scope, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", scope)
                    for scope in record["scopes"]
                )
            ):
                raise ValueError
            return record
        except (TypeError, ValueError, RecursionError):
            raise MarketplaceError(
                "MARKETPLACE_OAUTH_REQUIRED", "Authorize this TikTok Shop seller first.",
            ) from None

    async def _exchange_oauth_token(
        self, path: str, credentials: TikTokShopOAuthCredentials,
        token_field: str, token_value: str, check_current: Callable[[], None],
    ) -> dict[str, Any]:
        query = urlencode({
            "app_key": credentials.app_key.get_secret_value(),
            "app_secret": credentials.app_secret.get_secret_value(),
            token_field: token_value,
            "grant_type": "authorized_code" if token_field == "auth_code" else "refresh_token",
        })
        try:
            async with asyncio.timeout(30):
                destination = await pin_marketplace_destination(TIKTOK_OAUTH_ORIGIN + path)
                check_current()
                wire_request = httpx.Request(
                    "GET", destination.request_url + "?" + query,
                    headers={
                        "Host": destination.host_header,
                        "Accept": "application/json", "Accept-Encoding": "identity",
                    },
                    extensions={
                        "sni_hostname": destination.sni_hostname,
                        "timeout": {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 5.0},
                    },
                )
                content, _ = await send_marketplace_request(
                    self.transport_factory, wire_request,
                    lambda status: MarketplaceError(
                        "MARKETPLACE_OAUTH_REJECTED",
                        "TikTok Shop refused the seller token. Reauthorize the seller.",
                        403 if status in {400, 401, 403} else 502,
                    ),
                )
                check_current()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED", "TikTok Shop authorization request failed.", 502,
            ) from None
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                raise ValueError
            if payload["code"] != 0:
                raise MarketplaceError(
                    "MARKETPLACE_OAUTH_REJECTED",
                    "TikTok Shop refused the seller token. Reauthorize the seller.", 403,
                )
            data = payload["data"]
            if not isinstance(data, dict) or type(data.get("user_type")) is not int:
                raise ValueError
            if data["user_type"] != 0:
                raise MarketplaceError(
                    "MARKETPLACE_OAUTH_INVALID",
                    "TikTok Shop returned a non-seller authorization.", 403,
                )
            scopes = data["granted_scopes"]
            if (
                not isinstance(scopes, list) or not 1 <= len(scopes) <= 200
                or not all(
                    isinstance(scope, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", scope)
                    for scope in scopes
                )
            ):
                raise ValueError
            record = {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "open_id": data["open_id"],
                "expires_at": data["access_token_expire_in"],
                "refresh_expires_at": data["refresh_token_expire_in"],
                "scopes": sorted(set(scopes)),
            }
            if (
                not all(
                    isinstance(record[name], str)
                    and 1 <= len(record[name]) <= 4096
                    and re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", record[name])
                    for name in ("access_token", "refresh_token", "open_id")
                )
                or any(
                    type(record[name]) is not int
                    or not self.clock() + _TIKTOK_REFRESH_MARGIN < record[name]
                    or record[name] >= 100_000_000_000
                    for name in ("expires_at", "refresh_expires_at")
                )
            ):
                raise ValueError
            return record
        except (KeyError, TypeError, ValueError, RecursionError):
            raise _protocol_error() from None

    async def _oauth_access_token(
        self, connector_id: str, runtime: CommercialConnectorRuntime,
        credentials: TikTokShopOAuthCredentials, check_current: Callable[[], None],
    ) -> dict[str, Any]:
        _, token_key = tiktok_oauth_vault_keys(connector_id)
        async with self._oauth_lock(connector_id):
            check_current()
            record = self._stored_oauth_token(connector_id, runtime)
            if record["expires_at"] <= self.clock() + _TIKTOK_REFRESH_MARGIN:
                if record["refresh_expires_at"] <= self.clock() + _TIKTOK_REFRESH_MARGIN:
                    raise MarketplaceError(
                        "MARKETPLACE_OAUTH_REQUIRED",
                        "TikTok Shop authorization expired. Reauthorize the seller.",
                    )
                replacement = await self._exchange_oauth_token(
                    TIKTOK_REFRESH_PATH, credentials, "refresh_token",
                    record["refresh_token"], check_current,
                )
                if replacement["open_id"] != record["open_id"]:
                    raise MarketplaceError(
                        "MARKETPLACE_OAUTH_INVALID", "TikTok Shop seller identity changed.", 403,
                    )
                with self.connectors._transaction():
                    check_current()
                    self.connectors.vault.set(token_key, json.dumps({
                        **replacement, "revision": runtime.connector.revision,
                    }))
                record = replacement
            return record

    def identity_parts(self, connector_id: str) -> list[str | None]:
        runtime = self.connectors.require_tiktok_shop_runtime(connector_id)
        credentials = parse_tiktok_shop_credentials(runtime.credential.get_secret_value())
        return [runtime.connector.type, runtime.connector.endpoint,
                credentials.app_key.get_secret_value()]

    def require_grantable(self, connector_id: str) -> None:
        runtime = self.connectors.require_tiktok_shop_runtime(connector_id)
        if TIKTOK_PRODUCT_SCOPE not in runtime.connector.scope.split():
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Configure seller product read permission first.",
            )

    async def read(
        self, connector_id: str, request: MarketplaceReadRequest, *,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_tiktok_shop_runtime(connector_id)
        configured_credentials = parse_tiktok_shop_credentials(
            runtime.credential.get_secret_value(),
        )
        if request.operation == "orders":
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED", "TikTok Shop order reads are not available.",
            )
        if request.product_status not in TIKTOK_PRODUCT_STATUSES:
            raise MarketplaceError(
                "MARKETPLACE_FILTER_UNSUPPORTED", "Use a TikTok Shop product status filter.",
            )
        required = {TIKTOK_AUTH_SCOPE}
        if request.operation == "products":
            required.add(TIKTOK_PRODUCT_SCOPE)
        if not required.issubset(set(runtime.connector.scope.split())):
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED",
                "Required seller read permissions are not configured.",
            )

        def check_configuration() -> None:
            if authorize is not None:
                authorize()
            current = self.connectors.require_tiktok_shop_runtime(connector_id)
            if current != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "The connector changed during the request. Read again with current settings.",
                    409,
                )

        try:
            async with asyncio.timeout(30):
                if isinstance(configured_credentials, TikTokShopOAuthCredentials):
                    token = await self._oauth_access_token(
                        connector_id, runtime, configured_credentials, check_configuration,
                    )
                    if not required.issubset(set(token["scopes"])):
                        raise MarketplaceError(
                            "MARKETPLACE_SCOPE_REQUIRED",
                            "TikTok Shop no longer grants the required seller read permissions.",
                            403,
                        )
                    credentials = TikTokShopCredentials(
                        app_key=configured_credentials.app_key,
                        app_secret=configured_credentials.app_secret,
                        access_token=SecretStr(token["access_token"]),
                        access_token_expires_at=token["expires_at"],
                    )
                else:
                    credentials = configured_credentials
                private_values = {
                    secret.get_secret_value()
                    for secret in (
                        credentials.app_key, credentials.app_secret, credentials.access_token,
                    )
                }

                def check_current() -> None:
                    check_configuration()
                    if credentials.access_token_expires_at <= self.clock() + 30:
                        raise MarketplaceError(
                            "MARKETPLACE_TOKEN_EXPIRED",
                            "The seller access token expired. Reauthorize or read again.",
                        )

                check_current()
                shops_payload = await self._request(
                    credentials, "GET", TIKTOK_SHOPS_PATH, {}, b"", check_current, private_values,
                )
                shops = shops_payload["data"].get("shops")
                if not isinstance(shops, list) or len(shops) > 100:
                    raise _protocol_error()
                safe_shops: list[dict[str, str]] = []
                cipher = None
                seen: set[str] = set()
                for shop in shops:
                    if not isinstance(shop, dict):
                        raise _protocol_error()
                    shop_id = shop.get("id")
                    if (
                        not isinstance(shop_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", shop_id)
                        or shop_id in seen
                    ):
                        raise _protocol_error()
                    seen.add(shop_id)
                    shop_cipher = shop.get("cipher")
                    if isinstance(shop_cipher, str) and shop_cipher:
                        if not re.fullmatch(r"[A-Za-z0-9_=-]{1,512}", shop_cipher):
                            raise _protocol_error()
                        private_values.add(shop_cipher)
                    safe_shops.append({
                        "id": shop_id,
                        "name": self._text(shop.get("name"), credentials, 1000),
                        "region": self._text(shop.get("region", ""), credentials, 80),
                        "seller_type": self._text(shop.get("seller_type", ""), credentials, 80),
                    })
                    if shop_id == request.shop_id:
                        cipher = shop.get("cipher")
                if any(private in shop["id"] for shop in safe_shops for private in private_values):
                    raise _protocol_error()
                if request.operation == "authorized_shops":
                    result = {"shops": safe_shops, "products": [], "next_page_token": None}
                    payload = shops_payload
                    path = TIKTOK_SHOPS_PATH
                else:
                    if request.shop_id not in seen:
                        raise MarketplaceError(
                            "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                            "The seller token does not authorize this shop.", 403,
                        )
                    if not isinstance(cipher, str) or not re.fullmatch(
                        r"[A-Za-z0-9_=-]{1,512}", cipher,
                    ):
                        raise _protocol_error()
                    parameters = {"shop_cipher": cipher, "page_size": str(request.page_size)}
                    if request.page_token is not None:
                        parameters["page_token"] = request.page_token
                    body = json.dumps(
                        {"status": request.product_status}, separators=(",", ":"),
                    ).encode("utf-8")
                    path = TIKTOK_PRODUCTS_PATH
                    payload = await self._request(
                        credentials, "POST", path, parameters, body, check_current, private_values,
                    )
                    products = payload["data"].get("products")
                    if not isinstance(products, list) or len(products) > request.page_size:
                        raise _protocol_error()
                    safe_products = []
                    product_ids: set[str] = set()
                    for item in products:
                        if not isinstance(item, dict):
                            raise _protocol_error()
                        product_id = item.get("id")
                        if (
                            not isinstance(product_id, str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", product_id)
                            or product_id in product_ids
                            or any(private in product_id for private in private_values)
                        ):
                            raise _protocol_error()
                        product_ids.add(product_id)
                        safe_products.append({
                            "id": product_id,
                            "title": self._text(item.get("title"), credentials, 2000),
                            "status": self._text(item.get("status"), credentials, 80),
                        })
                    cursor = payload["data"].get("next_page_token") or None
                    if cursor is not None and (
                        not isinstance(cursor, str) or len(cursor) > 2048
                        or any(ord(char) < 32 for char in cursor)
                    ):
                        raise _protocol_error()
                    # An opaque cursor must round-trip unchanged; reject reflected secrets.
                    if cursor is not None and any(
                        secret.get_secret_value() in cursor
                        for secret in (credentials.app_secret, credentials.access_token)
                    ):
                        raise _protocol_error()
                    result = {
                        "shops": [], "products": safe_products, "next_page_token": cursor,
                    }
                check_current()
                # Opaque IDs/cursors must remain exact. Reject reflected private
                # values there; redact them from free text, including server echoes
                # of a shop cipher or the generated request signature.
                for collection in (result["shops"], result["products"]):
                    for item in collection:
                        for key, value in item.items():
                            for private in private_values:
                                if private in value:
                                    if key == "id":
                                        raise _protocol_error()
                                    value = value.replace(private, "[redacted]")
                            item[key] = value
                cursor = result["next_page_token"]
                if cursor is not None and any(private in cursor for private in private_values):
                    raise _protocol_error()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED", "The marketplace request failed. Try reading again.",
                502,
            ) from None
        request_id = self._text(payload.get("request_id", ""), credentials, 160)
        for private in private_values:
            request_id = request_id.replace(private, "[redacted]")
        return {
            "connector_id": connector_id, "operation": request.operation,
            "shop_id": request.shop_id, **result,
            "source_url": TIKTOK_SHOP_ORIGIN + path,
            "retrieved_at": utc_now().isoformat(),
            "request_id": request_id,
            "data_scope": "authorized_seller_data",
        }

    @staticmethod
    def _text(value: Any, credentials: TikTokShopCredentials, limit: int) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise _protocol_error()
        for secret in (credentials.app_key, credentials.app_secret, credentials.access_token):
            value = value.replace(secret.get_secret_value(), "[redacted]")
        return sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD).text

    async def _request(
        self, credentials: TikTokShopCredentials, method: str, path: str,
        parameters: dict[str, str], body: bytes, check_current: Callable[[], None],
        private_values: set[str],
    ) -> dict[str, Any]:
        destination = await pin_marketplace_destination(TIKTOK_SHOP_ORIGIN + path)
        check_current()
        parameters = {
            **parameters, "app_key": credentials.app_key.get_secret_value(),
            "timestamp": str(int(self.clock())),
        }
        parameters["sign"] = sign_tiktok_shop_request(
            path, parameters, body, credentials.app_secret.get_secret_value(),
        )
        private_values.add(parameters["sign"])
        wire_request = httpx.Request(
            method, destination.request_url + "?" + urlencode(parameters), content=body,
            headers={
                "Host": destination.host_header, "Content-Type": "application/json",
                "Accept": "application/json", "Accept-Encoding": "identity",
                "x-tts-access-token": credentials.access_token.get_secret_value(),
            },
            extensions={
                "sni_hostname": destination.sni_hostname,
                "timeout": {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 5.0},
            },
        )
        content, _headers = await send_marketplace_request(
            self.transport_factory, wire_request,
            lambda status: MarketplaceError(
                "MARKETPLACE_HTTP_REJECTED",
                f"The marketplace rejected the read (HTTP {status}).", 502,
            ),
        )
        check_current()
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            raise _protocol_error() from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise _protocol_error()
        if not 0 <= payload["code"] <= 999_999_999:
            raise _protocol_error()
        if payload["code"] != 0:
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                f"The marketplace rejected the read (API code {payload['code']}). "
                "Check seller authorization, granted scopes and token validity.", 502,
            )
        if not isinstance(payload.get("data"), dict):
            raise _protocol_error()
        return payload
