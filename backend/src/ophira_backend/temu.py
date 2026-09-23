"""Temu local-seller reads using explicit regional gateways and live token scopes.

Supply prices and reported stock retain the upstream meanings; neither implies
retail price, reserved stock, market demand or inventory forecasts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
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
from .untrusted import UntrustedSource, sanitize_untrusted_text

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

TEMU_TYPE = "temu"
TEMU_ORIGIN = "https://openapi-b-global.temu.com"
TEMU_ORIGINS = frozenset(
    {
        TEMU_ORIGIN,
        "https://openapi-b-us.temu.com",
        "https://openapi-b-eu.temu.com",
    }
)
TEMU_PATH = "/openapi/router"
TEMU_TOKEN_API = "bg.open.accesstoken.info.get"
TEMU_PRODUCTS_API = "temu.local.goods.list.retrieve"
TEMU_SKUS_API = "temu.local.sku.list.retrieve"
TEMU_PRICES_API = "bg.local.goods.sku.list.price.query"
# These are OPHIRA permissions. API names from token info are checked separately.
TEMU_STORE_SCOPE = "temu:store:read"
TEMU_PRODUCT_SCOPE = "temu:products:read"
TEMU_PRICE_SCOPE = "temu:supply-prices:read"
TEMU_DEFAULT_SCOPE = f"{TEMU_STORE_SCOPE} {TEMU_PRODUCT_SCOPE}"
TEMU_STATUSES = frozenset({"ALL", "ACTIVE", "INACTIVE", "INCOMPLETE", "DRAFT", "DELETED"})
MAX_TEMU_PAGE_SIZE = 25  # Current reference ambiguously says "100 per page, 25 per page".


class TemuCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    app_key: SecretStr = Field(min_length=1, max_length=160)
    app_secret: SecretStr = Field(min_length=1, max_length=512)
    access_token: SecretStr = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def credential_encoding(self):
        for value in (self.app_key, self.app_secret, self.access_token):
            if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", value.get_secret_value()):
                raise ValueError("invalid credential encoding")
        return self


def parse_temu_credentials(raw):
    try:
        if len(raw.encode()) > 8000:
            raise ValueError
        return TemuCredentials.model_validate(parse_json(raw.encode()))
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            "Temu requires an app key, app secret and authorized seller token"
        ) from None


def sign_temu_request(parameters, app_secret):
    """Match the official Python example: compact JSON values, sorted outer keys."""
    values = [
        key + json.dumps(parameters[key], ensure_ascii=False, separators=(",", ":")).strip('"')
        for key in sorted(parameters)
        if key != "sign"
    ]
    # MD5 is required by Temu's wire protocol, not used as a local security primitive.
    return hashlib.md5((app_secret + "".join(values) + app_secret).encode()).hexdigest().upper()


def _invalid():
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID", "Temu returned an invalid or oversized response.", 502
    )


def _object(value):
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _array(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise _invalid()
    return value


def _integer(value, *, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _identifier(value, private):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
        or any(secret in value for secret in private)
    ):
        raise _invalid()
    return value


def _text(value, private, maximum=2000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise _invalid()
    pattern = "|".join(re.escape(secret) for secret in sorted(private, key=len, reverse=True))
    value = re.sub(pattern, "[redacted]", value)
    sanitized = sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD)
    if sanitized.truncated:
        raise _invalid()
    return sanitized.text


def _long(value):
    """Only the legacy supply-price endpoint requires numeric IDs."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        return None
    result = int(value)
    return result if result < 2**63 else None


def _page(data, key, page_size, token, private):
    items = _array(data.get(key), page_size)
    total = _integer(data.get("total"))
    if len(items) > total:
        raise _invalid()
    pagination = data.get("pagination")
    if pagination is None:
        pagination = {}
    following = _object(pagination).get("nextToken")
    if following is not None:
        if (
            not isinstance(following, str)
            or not 1 <= len(following) <= 2048
            or not following.isprintable()
            or following == token
            or any(secret in following for secret in private)
            or not items
        ):
            raise _invalid()
    return items, total, following


class TemuReader:
    def __init__(self, connectors: ConnectorRegistry, *, transport_factory=None, clock=time.time):
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock

    def identity_parts(self, connector_id):
        runtime = self.connectors.require_temu_runtime(connector_id)
        credentials = parse_temu_credentials(runtime.credential.get_secret_value())
        return [TEMU_TYPE, runtime.connector.endpoint, credentials.app_key.get_secret_value()]

    def require_grantable(self, connector_id):
        runtime = self.connectors.require_temu_runtime(connector_id)
        if not {TEMU_STORE_SCOPE, TEMU_PRODUCT_SCOPE}.issubset(runtime.connector.scope.split()):
            raise MarketplaceError("MARKETPLACE_SCOPE_REQUIRED", "Allow Temu product reads first.")

    async def read(self, connector_id, request: MarketplaceReadRequest, *, authorize=None):
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_temu_runtime(connector_id)
        credentials = parse_temu_credentials(runtime.credential.get_secret_value())
        if request.operation not in {"authorized_shops", "products", "product_details"}:
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED", "This Temu read operation is not available."
            )
        if request.product_status not in TEMU_STATUSES:
            raise MarketplaceError("MARKETPLACE_FILTER_UNSUPPORTED", "Use a Temu product status.")
        if request.operation != "authorized_shops" and request.page_size > MAX_TEMU_PAGE_SIZE:
            raise MarketplaceError(
                "MARKETPLACE_PAGE_SIZE_INVALID", "Temu pages allow at most 25 items."
            )
        if request.page_token is not None and not request.page_token.isprintable():
            raise MarketplaceError(
                "MARKETPLACE_CURSOR_INVALID", "Use the returned Temu page token."
            )
        required = {TEMU_STORE_SCOPE}
        if request.operation != "authorized_shops":
            required.add(TEMU_PRODUCT_SCOPE)
        if not required.issubset(runtime.connector.scope.split()):
            raise MarketplaceError("MARKETPLACE_SCOPE_REQUIRED", "Allow the required Temu reads.")
        private = {
            value.get_secret_value()
            for value in (credentials.app_key, credentials.app_secret, credentials.access_token)
        }

        def current():
            if authorize is not None:
                authorize()
            if self.connectors.require_temu_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "Temu credentials changed. Read again.",
                    409,
                )

        sources = []
        origin = runtime.connector.endpoint

        async def get(api, parameters):
            payload = await self._request(origin, api, parameters, credentials, private, current)
            sources.append(
                {
                    "source_url": origin + TEMU_PATH,
                    "api_name": api,
                    "request_id": _text(payload.get("requestId"), private, 160),
                }
            )
            return payload["result"]

        try:
            async with asyncio.timeout(30):
                current()
                info = await get(TEMU_TOKEN_API, {})
                mall_id = str(_integer(info.get("mallId"), minimum=1, maximum=2**63 - 1))
                expires = _integer(info.get("expiredTime"), minimum=1, maximum=100_000_000_000)
                if expires <= self.clock():
                    raise MarketplaceError(
                        "MARKETPLACE_TOKEN_EXPIRED", "Replace the expired Temu seller token.", 401
                    )
                mall_type = _integer(info.get("mallType"))
                if mall_type not in {1, 100}:
                    raise _invalid()
                scopes = _array(info.get("apiScopeList"), 3000)
                if any(
                    not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,160}", scope)
                    for scope in scopes
                ):
                    raise _invalid()
                shop = {
                    "id": mall_id,
                    "name": "",
                    "region": "",
                    "seller_type": "LOCAL" if mall_type == 100 else "SEMI",
                    "product_reads_available": mall_type == 100 and TEMU_PRODUCTS_API in scopes,
                    "sku_reads_available": mall_type == 100 and TEMU_SKUS_API in scopes,
                }
                # Token info returns a region ID, not a shop name or an ISO region code.
                if info.get("regionId") is not None:
                    shop["region_id"] = str(_integer(info["regionId"], minimum=1))
                result = {
                    "shops": [shop],
                    "products": [],
                    "next_page_token": None,
                    "authorization_expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
                }
                partial = False
                if request.operation != "authorized_shops":
                    if request.shop_id != mall_id:
                        raise MarketplaceError(
                            "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                            "This token belongs to another shop.",
                            403,
                        )
                    if mall_type != 100:
                        raise MarketplaceError(
                            "MARKETPLACE_SELLER_TYPE_UNSUPPORTED",
                            "Temu product reads currently require a local-seller shop.",
                        )
                    upstream = {TEMU_PRODUCTS_API}
                    if request.operation == "product_details":
                        upstream.add(TEMU_SKUS_API)
                    if not upstream.issubset(scopes):
                        raise MarketplaceError(
                            "MARKETPLACE_UPSTREAM_SCOPE_REQUIRED",
                            "Authorize the required product/SKU APIs in Temu Seller Center.",
                            403,
                        )
                    if request.operation == "products":
                        parameters = {
                            "pageSize": request.page_size,
                            "goodsSearchType": request.product_status,
                        }
                        if request.page_token is not None:
                            parameters["pageToken"] = request.page_token
                        data = await get(TEMU_PRODUCTS_API, parameters)
                        items, total, following = _page(
                            data, "goodsList", request.page_size, request.page_token, private
                        )
                        products = [self._product(_object(item), private) for item in items]
                        if len({item["id"] for item in products}) != len(products):
                            raise _invalid()
                        partial = (
                            request.page_token is None and following is None and len(items) != total
                        )
                        result = {
                            "shops": [],
                            "products": products,
                            "total_count": total,
                            "next_page_token": following,
                        }
                    else:
                        base = await get(
                            TEMU_PRODUCTS_API,
                            {
                                "goodsIdList": [request.product_id],
                                "pageSize": 1,
                            },
                        )
                        items, base_total, following = _page(base, "goodsList", 1, None, private)
                        if not items:
                            raise MarketplaceError(
                                "MARKETPLACE_PRODUCT_UNAVAILABLE",
                                "Temu did not return this product.",
                                404,
                            )
                        product = self._product(_object(items[0]), private)
                        if (
                            product["id"] != request.product_id
                            or following is not None
                            or base_total != 1
                        ):
                            raise _invalid()
                        parameters = {
                            "goodsIdList": [request.product_id],
                            "pageSize": request.page_size,
                        }
                        if request.page_token is not None:
                            parameters["pageToken"] = request.page_token
                        data = await get(TEMU_SKUS_API, parameters)
                        items, total, following = _page(
                            data, "skuList", request.page_size, request.page_token, private
                        )
                        variants = [
                            self._sku(_object(item), product["id"], private) for item in items
                        ]
                        if len({item["id"] for item in variants}) != len(variants):
                            raise _invalid()
                        price_access = "disabled"
                        if TEMU_PRICE_SCOPE in runtime.connector.scope.split():
                            price_access = (
                                "allowed" if TEMU_PRICES_API in scopes else "not_authorized"
                            )
                        if price_access == "allowed" and variants:
                            if _long(product["id"]) is None or any(
                                _long(v["id"]) is None for v in variants
                            ):
                                price_access = "unsupported_identifier"
                                partial = True
                            else:
                                prices = await get(
                                    TEMU_PRICES_API,
                                    {
                                        "querySupplierPriceBaseList": [
                                            {
                                                "goodsId": int(product["id"]),
                                                "skuIdList": [int(v["id"]) for v in variants],
                                            }
                                        ]
                                    },
                                )
                                mapped = self._prices(prices, product["id"], variants, private)
                                for variant in variants:
                                    variant["supplier_price"] = mapped.get(variant["id"])
                                partial = len(mapped) != len(variants)
                        product.update(
                            variants=variants,
                            variants_loaded=True,
                            variants_complete=(
                                request.page_token is None
                                and following is None
                                and len(variants) == total
                            ),
                        )
                        partial |= (
                            request.page_token is None
                            and following is None
                            and len(variants) != total
                        )
                        result = {
                            "shops": [],
                            "products": [product],
                            "total_count": total,
                            "pagination_unit": "SKU",
                            "next_page_token": following,
                            "supplier_price_access": price_access,
                        }
                current()
                if expires <= self.clock():
                    raise MarketplaceError(
                        "MARKETPLACE_TOKEN_EXPIRED",
                        "The Temu authorization expired during the read.",
                        401,
                    )
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED", "The Temu read failed. Try reading again.", 502
            ) from None
        return {
            "connector_id": connector_id,
            "operation": request.operation,
            "shop_id": request.shop_id,
            **result,
            "partial_data": partial,
            "source_requests": sources,
            **sources[-1],
            "retrieved_at": utc_now().isoformat(),
            "granted_scopes": [
                api for api in (TEMU_PRODUCTS_API, TEMU_SKUS_API, TEMU_PRICES_API) if api in scopes
            ],
            "data_scope": "authorized_seller_data",
        }

    @staticmethod
    def _product(item, private):
        value = {
            "id": _identifier(item.get("goodsId"), private),
            "title": _text(item.get("goodsName"), private),
            "status": _text(item.get("goodsStatus"), private, 80) or "UNKNOWN",
            "sku": _text(item.get("outGoodsSn"), private, 200),
            "category_id": _text(item.get("catId"), private, 160),
            "details_available": True,
            "variants_loaded": False,
        }
        if item.get("variationsCount") is not None:
            value["reported_sku_count"] = _integer(item["variationsCount"])
        return value

    @staticmethod
    def _sku(item, product_id, private):
        if _identifier(item.get("goodsId"), private) != product_id:
            raise _invalid()
        options = [
            {
                "name": _text(_object(option).get("parentSpecName"), private, 100),
                "value": _text(option.get("specName"), private, 200),
            }
            for option in _array(item.get("specList", []), 10)
        ]
        stock = item.get("stock")
        return {
            "id": _identifier(item.get("skuId"), private),
            "title": _text(item.get("specName"), private)
            or " / ".join(v["value"] for v in options),
            "status": _text(item.get("skuStatus"), private, 80) or "UNKNOWN",
            "sku": _text(item.get("outSkuSn"), private, 200),
            "options": options,
            "reported_stock": None if stock is None else _integer(stock),
            "supplier_price": None,
        }

    @staticmethod
    def _prices(data, product_id, variants, private):
        result = {}
        ids = {item["id"] for item in variants}
        seen = set()
        for group in _array(data.get("openapiGoodsSupplierPriceDTOList"), 1):
            group = _object(group)
            if str(_integer(group.get("goodsId"), minimum=1, maximum=2**63 - 1)) != product_id:
                raise _invalid()
            for item in _array(group.get("openapiSkuSupplierPriceDTOList"), len(ids)):
                item = _object(item)
                sku_id = str(_integer(item.get("skuId"), minimum=1, maximum=2**63 - 1))
                if sku_id not in ids or sku_id in seen:
                    raise _invalid()
                seen.add(sku_id)
                price = item.get("supplierPrice")
                if price is None:
                    continue
                price = _object(price)
                amount, currency = price.get("amount"), price.get("currency")
                if amount is None or currency is None:
                    continue
                if (
                    not isinstance(amount, str)
                    or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,8})?", amount)
                    or not isinstance(currency, str)
                    or not re.fullmatch(r"[A-Z]{3}", currency)
                ):
                    raise _invalid()
                result[sku_id] = {"amount": amount, "currency": currency}
        return result

    async def _request(self, origin, api, parameters, credentials, private, current):
        destination = await pin_marketplace_destination(origin + TEMU_PATH)
        current()
        body = {
            "type": api,
            "app_key": credentials.app_key.get_secret_value(),
            "access_token": credentials.access_token.get_secret_value(),
            "timestamp": str(int(self.clock())),
            "data_type": "JSON",
            **parameters,
        }
        signature = sign_temu_request(body, credentials.app_secret.get_secret_value())
        private.add(signature)
        body["sign"] = signature
        request = httpx.Request(
            "POST",
            destination.request_url,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={
                "Host": destination.host_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
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
                "MARKETPLACE_HTTP_REJECTED", f"Temu rejected the read (HTTP {status}).", 502
            ),
        )
        current()
        try:
            payload = _object(parse_json(content))
        except (ValueError, RecursionError):
            raise _invalid() from None
        if type(payload.get("success")) is not bool or type(payload.get("errorCode")) is not int:
            raise _invalid()
        if not payload["success"] or payload["errorCode"] != 1000000:
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                "Temu rejected the read. Check seller authorization.",
                502,
            )
        _object(payload.get("result"))
        return payload
