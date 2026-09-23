"""Official marketplace contracts and a bounded, seller-authorized TikTok Shop reader.

Only the documented read operations below are callable. Credential material,
shop ciphers, signed URLs and upstream error bodies never leave this adapter.
The Shopify reader in ``shopify.py`` shares the contracts and transport bounds here.
See docs/marketplace-connectors.md for source versions and acceptance limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

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
    from .connectors import ConnectorRegistry
    from .web_tools import PinnedPublicDestination

TIKTOK_SHOP_TYPE = "tiktok_shop"
SHOPIFY_TYPE = "shopify"
TIKTOK_SHOP_ORIGIN = "https://open-api.tiktokglobalshop.com"
TIKTOK_SHOPS_PATH = "/authorization/202309/shops"
TIKTOK_PRODUCTS_PATH = "/product/202502/products/search"
TIKTOK_AUTH_SCOPE = "seller.authorization.info"
TIKTOK_PRODUCT_SCOPE = "seller.product.basic"
MAX_MARKETPLACE_RESPONSE_BYTES = 2 * 1024 * 1024

MARKETPLACE_GUIDES = [
    {
        "type": "shein", "name": "SHEIN", "read_adapter": True,
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
        "type": "shopee", "name": "Shopee", "read_adapter": True,
        "documentation_url": "https://open.shopee.cn/developer-guide/12",
    },
    {
        "type": SHOPIFY_TYPE, "name": "Shopify", "read_adapter": True,
        "documentation_url": "https://shopify.dev/docs/apps/build/authentication-authorization/"
        "client-credentials-grant",
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


def parse_tiktok_shop_credentials(raw: str) -> TikTokShopCredentials:
    try:
        if len(raw) > 8000:
            raise ValueError("too large")
        return TikTokShopCredentials.model_validate_json(raw)
    except (ValidationError, ValueError):
        # Pydantic errors include input_value, so never relay their text.
        raise ValueError("TikTok Shop requires a valid seller credential bundle") from None


ProductStatus = Literal[
    "ALL", "DRAFT", "PENDING", "FAILED", "ACTIVATE", "SELLER_DEACTIVATED",
    "PLATFORM_DEACTIVATED", "FREEZE", "DELETED", "ACTIVE", "ARCHIVED", "UNLISTED",
    "ON_SHELF", "OFF_SHELF",
    "NORMAL", "BANNED", "UNLIST", "REVIEWING", "SELLER_DELETE", "SHOPEE_DELETE",
]
# Each platform documents its own status vocabulary; filters never cross platforms.
TIKTOK_PRODUCT_STATUSES = frozenset({
    "ALL", "DRAFT", "PENDING", "FAILED", "ACTIVATE", "SELLER_DEACTIVATED",
    "PLATFORM_DEACTIVATED", "FREEZE", "DELETED",
})
SHOPIFY_PRODUCT_STATUSES = frozenset({"ALL", "ACTIVE", "ARCHIVED", "DRAFT", "UNLISTED"})


class MarketplaceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["authorized_shops", "products", "orders", "product_details"]
    shop_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    page_size: int = Field(default=20, ge=1, le=100)
    page_token: str | None = Field(default=None, min_length=1, max_length=2048)
    product_status: ProductStatus = "ALL"
    product_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def require_shop(self) -> MarketplaceReadRequest:
        if self.operation in {"products", "orders", "product_details"} and not self.shop_id:
            raise ValueError("Select an authorized shop before reading products or orders")
        if self.operation == "product_details":
            if not self.product_id or self.product_status != "ALL":
                raise ValueError("Product details require an item id and no status filter")
        elif self.product_id is not None:
            raise ValueError("Only product details accept an item id")
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
        credentials = parse_tiktok_shop_credentials(runtime.credential.get_secret_value())
        private_values = {
            secret.get_secret_value()
            for secret in (credentials.app_key, credentials.app_secret, credentials.access_token)
        }
        if request.operation not in {"authorized_shops", "products"}:
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED", "This TikTok Shop read is not available.",
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

        def check_current() -> None:
            if authorize is not None:
                authorize()
            current = self.connectors.require_tiktok_shop_runtime(connector_id)
            if current != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "The connector changed during the request. Read again with current settings.",
                    409,
                )
            if credentials.access_token_expires_at <= self.clock() + 30:
                raise MarketplaceError(
                    "MARKETPLACE_TOKEN_EXPIRED", "Replace the expired seller access token.",
                )

        try:
            async with asyncio.timeout(30):
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
