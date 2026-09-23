"""SHEIN seller reads using the official signature tool and fixed API contracts.

Products remain SPU groups with their SKC variants. No write, arbitrary API path,
credential exchange, upstream error body or signed header is exposed to callers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import string
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .backup_files import parse_json
from .marketplaces import (
    MarketplaceError,
    MarketplaceReadRequest,
    default_marketplace_transport,
    pin_marketplace_destination,
    send_marketplace_request,
)
from .models import utc_now
from .shein_catalog import (
    _identifier,
    _invalid,
    _status,
    _text,
    attach_inventory,
    normalize_details,
    page_skus,
    sku_cursor,
)

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

SHEIN_TYPE = "shein"
SHEIN_ORIGIN = "https://openapi.sheincorp.com"
SHEIN_ORIGINS = frozenset({SHEIN_ORIGIN, "https://openapi.sheincorp.cn"})
SHEIN_STORE_PATH = "/open-api/openapi-business-backend/query-store-info"
SHEIN_AUTH_PATH = "/open-api/auth/get-by-token"
SHEIN_PRODUCTS_PATH = "/open-api/goods/searchProduct"
SHEIN_DETAILS_PATH = "/open-api/goods/spu-info"
SHEIN_STOCK_PATH = "/open-api/stock/stock-query"
# Application permissions; these are not claims about upstream OAuth scopes.
SHEIN_STORE_SCOPE = "shein:store:read"
SHEIN_PRODUCT_SCOPE = "shein:products:read"
SHEIN_DEFAULT_SCOPE = f"{SHEIN_STORE_SCOPE} {SHEIN_PRODUCT_SCOPE}"
SHEIN_PRODUCT_STATUSES = frozenset({"ALL", "ON_SHELF", "OFF_SHELF"})
MAX_SHEIN_PAGE_SIZE = 10


class SheinCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    open_key_id: SecretStr = Field(min_length=1, max_length=160)
    secret_key: SecretStr = Field(min_length=1, max_length=512)
    authorization_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    bound_endpoint: str | None = None
    bound_scope: str | None = None
    supplier_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def authorization_binding(self):
        fields = (self.authorization_id, self.bound_endpoint, self.bound_scope, self.supplier_id)
        if any(value is not None for value in fields):
            if (
                any(value is None for value in fields)
                or self.bound_endpoint not in SHEIN_ORIGINS
                or self.bound_scope not in {SHEIN_STORE_SCOPE, SHEIN_DEFAULT_SCOPE}
                or int(self.supplier_id) >= 2**63
            ):
                raise ValueError("invalid SHEIN authorization binding")
        return self


def parse_shein_credentials(raw):
    try:
        if len(raw.encode()) > 8000:
            raise ValueError
        value = SheinCredentials.model_validate(parse_json(raw.encode()))
        for key in (value.open_key_id, value.secret_key):
            if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", key.get_secret_value()):
                raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ValueError("SHEIN requires an openKeyId and decrypted seller secretKey") from None


def sign_shein_request(open_key_id, secret_key, path, timestamp, random_key):
    """Base64 encodes the lowercase hex digest, not the raw HMAC bytes."""
    if not re.fullmatch(r"[A-Za-z0-9]{5}", random_key):
        raise ValueError("SHEIN signing nonce must contain five ASCII letters or digits")
    digest = hmac.new(
        (secret_key + random_key).encode(),
        f"{open_key_id}&{timestamp}&{path}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return random_key + base64.b64encode(digest.encode("ascii")).decode("ascii")


class SheinReader:
    def __init__(
        self,
        connectors: ConnectorRegistry,
        *,
        transport_factory=None,
        clock=time.time,
        random_key=None,
    ):
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock
        self.random_key = random_key or (
            lambda: "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(5))
        )

    def identity_parts(self, connector_id):
        runtime = self.connectors.require_shein_runtime(connector_id)
        credentials = parse_shein_credentials(runtime.credential.get_secret_value())
        return [SHEIN_TYPE, runtime.connector.endpoint, credentials.open_key_id.get_secret_value()]

    def require_grantable(self, connector_id):
        runtime = self.connectors.require_shein_runtime(connector_id)
        if not {SHEIN_STORE_SCOPE, SHEIN_PRODUCT_SCOPE}.issubset(runtime.connector.scope.split()):
            raise MarketplaceError("MARKETPLACE_SCOPE_REQUIRED", "Allow SHEIN product reads first.")

    async def read(self, connector_id, request: MarketplaceReadRequest, *, authorize=None):
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_shein_runtime(connector_id)
        credentials = parse_shein_credentials(runtime.credential.get_secret_value())
        private_values = {
            credentials.open_key_id.get_secret_value(),
            credentials.secret_key.get_secret_value(),
        }
        if request.operation not in {"authorized_shops", "products", "product_details"}:
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED", "This SHEIN read operation is not available."
            )
        if request.product_status not in SHEIN_PRODUCT_STATUSES:
            raise MarketplaceError(
                "MARKETPLACE_FILTER_UNSUPPORTED", "Use a SHEIN shelf status filter."
            )
        required = {SHEIN_STORE_SCOPE}
        if request.operation in {"products", "product_details"}:
            required.add(SHEIN_PRODUCT_SCOPE)
            if request.operation == "products" and request.page_size > MAX_SHEIN_PAGE_SIZE:
                raise MarketplaceError(
                    "MARKETPLACE_PAGE_SIZE_INVALID",
                    "SHEIN product pages contain at most 10 SPU groups.",
                )
        if not required.issubset(runtime.connector.scope.split()):
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Allow the required SHEIN reads first."
            )
        page = 1
        offset, expected_digest = 0, None
        if request.operation == "product_details":
            offset, expected_digest = sku_cursor(request.page_token)
        elif request.page_token is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,8}", request.page_token):
                raise MarketplaceError(
                    "MARKETPLACE_CURSOR_INVALID", "Use the returned SHEIN page token."
                )
            page = int(request.page_token)

        def check_current():
            if authorize is not None:
                authorize()
            if self.connectors.require_shein_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "The connector changed during the request. Read again with current settings.",
                    409,
                )

        try:
            async with asyncio.timeout(30):
                check_current()
                origin = runtime.connector.endpoint
                sources = []

                async def get(path, body):
                    payload = await self._request(
                        origin,
                        path,
                        body,
                        credentials,
                        private_values,
                        check_current,
                    )
                    sources.append(
                        {
                            "source_url": origin + path,
                            "request_id": _text(payload.get("traceId"), private_values, 160),
                        }
                    )
                    return payload

                store = await get(SHEIN_STORE_PATH, {})
                info = store["info"].get("storeInfo")
                if not isinstance(info, dict):
                    raise _invalid()
                supplier_id = info.get("supplierId")
                if type(supplier_id) is not int or not 0 < supplier_id < 2**63:
                    raise _invalid()
                if (
                    credentials.supplier_id is not None
                    and str(supplier_id) != credentials.supplier_id
                ):
                    raise MarketplaceError(
                        "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                        "The seller key no longer matches the authorized shop.",
                        403,
                    )
                shop = {
                    "id": _identifier(str(supplier_id), private_values),
                    "name": _text(info.get("storeName"), private_values, 1000),
                    "region": "",  # The documented store response does not report a region.
                    "seller_type": _text(info.get("supplierBusinessMode"), private_values, 80),
                }
                result = {"shops": [shop], "products": [], "next_page_token": None}
                partial = False
                if request.operation != "authorized_shops" and request.shop_id != shop["id"]:
                    raise MarketplaceError(
                        "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                        "The seller key does not authorize this shop.",
                        403,
                    )
                if request.operation == "products":
                    body = {"pageNum": page, "pageSize": request.page_size, "languageList": ["en"]}
                    if request.product_status != "ALL":
                        body["skcShelfStatus"] = 1 if request.product_status == "ON_SHELF" else 0
                    payload = await get(SHEIN_PRODUCTS_PATH, body)
                    result = self._products(
                        payload["info"], request.page_size, page, private_values
                    )
                elif request.operation == "product_details":
                    listing = await get(
                        SHEIN_PRODUCTS_PATH,
                        {
                            "pageNum": 1,
                            "pageSize": 1,
                            "languageList": ["en"],
                            "spuNameList": [request.product_id],
                        },
                    )
                    owned = self._products(listing["info"], 1, 1, private_values)
                    if not owned["products"]:
                        raise MarketplaceError(
                            "MARKETPLACE_PRODUCT_UNAVAILABLE",
                            "This SPU is not in the authorized seller's catalog.",
                            404,
                        )
                    if (
                        owned["total_count"] != 1
                        or owned["products"][0]["id"] != request.product_id
                    ):
                        raise _invalid()
                    detail = await get(
                        SHEIN_DETAILS_PATH,
                        {
                            "spuName": request.product_id,
                            "languageList": ["en"],
                        },
                    )
                    product, partial = normalize_details(
                        detail["info"],
                        owned["products"][0],
                        private_values,
                    )
                    if request.inventory_type is not None:
                        inventory = await get(
                            SHEIN_STOCK_PATH,
                            {
                                "spuNameList": [request.product_id],
                                "invType": request.inventory_type,
                            },
                        )
                        partial |= attach_inventory(inventory["info"], product, private_values)
                    result = page_skus(product, request, offset, expected_digest)
                check_current()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED", "The SHEIN request failed. Try reading again.", 502
            ) from None
        return {
            "connector_id": connector_id,
            "operation": request.operation,
            "shop_id": request.shop_id,
            **result,
            "source_requests": sources,
            **sources[-1],
            "partial_data": partial,
            "retrieved_at": utc_now().isoformat(),
            "data_scope": "authorized_seller_data",
        }

    @staticmethod
    def _products(info, page_size, page, private_values):
        meta, items = info.get("meta"), info.get("data")
        count = meta.get("count") if isinstance(meta, dict) else None
        if (
            type(count) is not int
            or not 0 <= count <= 1_000_000_000
            or not isinstance(items, list)
            or len(items) > page_size
            or len(items) > count
        ):
            raise _invalid()
        products, identifiers, variant_ids = [], set(), set()
        for item in items:
            if not isinstance(item, dict):
                raise _invalid()
            identifier = _identifier(item.get("spuName"), private_values)
            if identifier in identifiers:
                raise _invalid()
            identifiers.add(identifier)
            skcs = item.get("skcList")
            if skcs is None:
                skcs = []
            if not isinstance(skcs, list) or len(skcs) > 100:
                raise _invalid()
            variants = []
            for skc in skcs:
                if not isinstance(skc, dict):
                    raise _invalid()
                skc_id = _identifier(skc.get("skcName"), private_values)
                if skc_id in variant_ids:
                    raise _invalid()
                variant_ids.add(skc_id)
                titles = skc.get("skcTitle")
                if titles is None:
                    titles = []
                if not isinstance(titles, list) or len(titles) > 5:
                    raise _invalid()
                english = []
                for title in titles:
                    if not isinstance(title, dict) or not isinstance(title.get("language"), str):
                        raise _invalid()
                    if title["language"] == "en":
                        english.append(_text(title.get("title"), private_values))
                if len(english) > 1:
                    raise _invalid()
                variants.append(
                    {
                        "id": skc_id,
                        "title": english[0] if english else "",
                        "status": _status(skc.get("skcShelfStatus")),
                    }
                )
            products.append(
                {
                    "id": identifier,
                    "title": "",
                    "status": _status(item.get("spuShelfStatus")),
                    "category_id": _text(item.get("categoryId"), private_values, 160),
                    "variants": variants,
                }
            )
        return {
            "shops": [],
            "products": products,
            "next_page_token": str(page + 1) if items and page * page_size < count else None,
            "pagination_unit": "SPU",
            "page_number": page,
            "total_count": count,
        }

    async def _request(self, origin, path, body, credentials, private_values, check_current):
        destination = await pin_marketplace_destination(origin + path)
        check_current()
        timestamp = str(int(self.clock() * 1000))
        signature = sign_shein_request(
            credentials.open_key_id.get_secret_value(),
            credentials.secret_key.get_secret_value(),
            path,
            timestamp,
            self.random_key(),
        )
        private_values.add(signature)
        request = httpx.Request(
            "POST",
            destination.request_url,
            content=json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Host": destination.host_header,
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "language": "en",
                (
                    "x-lt-appid" if path == SHEIN_AUTH_PATH else "x-lt-openKeyId"
                ): credentials.open_key_id.get_secret_value(),
                "x-lt-timestamp": timestamp,
                "x-lt-signature": signature,
            },
            extensions={
                "sni_hostname": destination.sni_hostname,
                "timeout": {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 5.0},
            },
        )
        content, _ = await send_marketplace_request(
            self.transport_factory,
            request,
            lambda status: MarketplaceError(
                "MARKETPLACE_HTTP_REJECTED", f"SHEIN rejected the read (HTTP {status}).", 502
            ),
        )
        check_current()
        try:
            parse_json(content)  # Reject duplicate keys before preserving decimal prices.
            payload = json.loads(content, parse_float=Decimal)
        except (ValueError, RecursionError, InvalidOperation):
            raise _invalid() from None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("code"), str)
            or not re.fullmatch(r"[0-9]{1,10}", payload["code"])
        ):
            raise _invalid()
        if payload["code"] != "0":
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                "SHEIN rejected the read. Check seller authorization and keys.",
                502,
            )
        expected_type = (dict, list) if path == SHEIN_STOCK_PATH else dict
        if not isinstance(payload.get("info"), expected_type):
            raise _invalid()
        return payload
