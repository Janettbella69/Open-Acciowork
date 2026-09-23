"""Bounded Shopee shop/catalog reads and durable, single-use token rotation.

Official v2 contracts and documentation discrepancies: docs/shopee-connector.md.
Credentials bind exactly one shop; public app-wide shop discovery is not exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus

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
from .secret_vault import CredentialChangedError
from .untrusted import UntrustedSource, sanitize_untrusted_text

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

SHOPEE_TYPE = "shopee"
SHOPEE_ORIGIN = "https://partner.shopeemobile.com"
SHOPEE_ORIGINS = frozenset(
    {
        SHOPEE_ORIGIN,
        "https://openplatform.shopee.cn",
        "https://openplatform.shopee.com.br",
    }
)
SHOPEE_SHOP_PATH = "/api/v2/shop/get_shop_info"
SHOPEE_PRODUCTS_PATH = "/api/v2/product/get_item_list"
SHOPEE_DETAILS_PATH = "/api/v2/product/get_item_base_info"
SHOPEE_MODELS_PATH = "/api/v2/product/get_model_list"
SHOPEE_REFRESH_PATH = "/api/v2/auth/access_token/get"
# Local application grants, not upstream OAuth scope names.
SHOPEE_SHOP_SCOPE = "shopee:shop:read"
SHOPEE_PRODUCT_SCOPE = "shopee:products:read"
SHOPEE_DEFAULT_SCOPE = f"{SHOPEE_SHOP_SCOPE} {SHOPEE_PRODUCT_SCOPE}"
SHOPEE_STATUSES = ("NORMAL", "BANNED", "UNLIST", "REVIEWING", "SELLER_DELETE", "SHOPEE_DELETE")


class ShopeeCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    partner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    shop_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    partner_key: SecretStr = Field(min_length=1, max_length=512)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    access_token_expires_at: int = Field(gt=0, lt=100_000_000_000)
    refresh_token: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    refresh_token_expires_at: int | None = Field(default=None, gt=0, lt=100_000_000_000)
    # Persisted BEFORE exchange. A crash/timeout never reuses a possibly consumed token.
    refresh_pending: bool = False

    @model_validator(mode="after")
    def coherent(self):
        if any(int(value) >= 2**63 for value in (self.partner_id, self.shop_id)):
            raise ValueError("identifier out of range")
        if (self.refresh_token is None) != (self.refresh_token_expires_at is None):
            raise ValueError("refresh token requires its expiry")
        for value in (self.partner_key, self.access_token, self.refresh_token):
            if value is not None and not re.fullmatch(
                r"[A-Za-z0-9_.~:+/=-]+", value.get_secret_value()
            ):
                raise ValueError("invalid credential encoding")
        return self

    def vault_json(self):
        value = self.model_dump()
        for key in ("partner_key", "access_token", "refresh_token"):
            if value[key] is not None:
                value[key] = value[key].get_secret_value()
        return json.dumps(value, separators=(",", ":"))


def parse_shopee_credentials(raw):
    try:
        if len(raw.encode()) > 16000:
            raise ValueError
        return ShopeeCredentials.model_validate(parse_json(raw.encode()))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Shopee requires a valid single-shop seller credential bundle") from None


def sign_shopee_request(partner_key, partner_id, path, timestamp, access_token="", shop_id=""):
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


def _invalid():
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID", "Shopee returned an invalid or oversized response.", 502
    )


def _changed():
    return MarketplaceError(
        "MARKETPLACE_CONFIGURATION_CHANGED", "Shopee credentials changed. Read again.", 409
    )


def _integer(value, *, maximum=2**63 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _id(value):
    return str(_integer(value, minimum=1))


def _text(value, private, limit=2000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid()
    encoded = {
        form for secret in private for form in (secret, quote(secret, safe=""), quote_plus(secret))
    }
    if encoded:
        pattern = "|".join(re.escape(secret) for secret in sorted(encoded, key=len, reverse=True))
        value = re.sub(pattern, "[redacted]", value)
    cleaned = sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD)
    if cleaned.truncated:
        raise _invalid()
    return cleaned.text


def _object(value):
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid()
    return value


def _money(value):
    if value is None:
        return None
    if type(value) not in {int, Decimal}:
        raise _invalid()
    number = Decimal(value)
    if not number.is_finite() or not 0 <= number < 10**18 or number.as_tuple().exponent < -8:
        raise _invalid()
    return format(number, "f")


def _price_stock(item, private):
    prices = []
    raw_prices = item.get("price_info")
    for price in _array([] if raw_prices is None else raw_prices, 10):
        price = _object(price)
        currency = price.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise _invalid()
        prices.append(
            {
                "currency": currency,
                "current": _money(price.get("current_price")),
                "original": _money(price.get("original_price")),
            }
        )
    raw_stock = item.get("stock_info_v2")
    raw_summary = _object({} if raw_stock is None else raw_stock).get("summary_info")
    summary = _object({} if raw_summary is None else raw_summary)
    stock = {}
    for name in ("total_available_stock", "total_reserved_stock"):
        value = summary.get(name)
        stock[name] = None if value is None else _integer(value, maximum=2**53 - 1)
    return {"prices": prices, "stock": stock}


class ShopeeReader:
    def __init__(self, connectors: ConnectorRegistry, *, transport_factory=None, clock=time.time):
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock

    def identity_parts(self, connector_id):
        runtime = self.connectors.require_shopee_runtime(connector_id)
        credentials = parse_shopee_credentials(runtime.credential.get_secret_value())
        return [
            SHOPEE_TYPE,
            runtime.connector.endpoint,
            credentials.partner_id,
            credentials.shop_id,
        ]

    def require_grantable(self, connector_id):
        runtime = self.connectors.require_shopee_runtime(connector_id)
        if not {SHOPEE_SHOP_SCOPE, SHOPEE_PRODUCT_SCOPE}.issubset(runtime.connector.scope.split()):
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Allow Shopee product reads first."
            )

    async def read(self, connector_id, request: MarketplaceReadRequest, *, authorize=None):
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_shopee_runtime(connector_id)
        credentials = parse_shopee_credentials(runtime.credential.get_secret_value())
        if request.operation not in {"authorized_shops", "products", "product_details"}:
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED", "This Shopee read is not available."
            )
        if request.product_status not in {"ALL", *SHOPEE_STATUSES}:
            raise MarketplaceError("MARKETPLACE_FILTER_UNSUPPORTED", "Use a Shopee item status.")
        required = {SHOPEE_SHOP_SCOPE}
        if request.operation != "authorized_shops":
            required.add(SHOPEE_PRODUCT_SCOPE)
            if request.shop_id != credentials.shop_id:
                raise MarketplaceError(
                    "MARKETPLACE_SHOP_NOT_AUTHORIZED", "Select the credential's Shopee shop.", 403
                )
        if not required.issubset(runtime.connector.scope.split()):
            raise MarketplaceError("MARKETPLACE_SCOPE_REQUIRED", "Allow the required Shopee reads.")
        offset = 0
        if request.page_token is not None:
            if not re.fullmatch(r"0|[1-9][0-9]{0,8}", request.page_token):
                raise MarketplaceError("MARKETPLACE_CURSOR_INVALID", "Use the returned offset.")
            offset = int(request.page_token)
        if request.product_id and (
            not re.fullmatch(r"[1-9][0-9]{0,18}", request.product_id)
            or int(request.product_id) >= 2**63
        ):
            raise MarketplaceError("MARKETPLACE_PRODUCT_INVALID", "Use a valid Shopee item id.")

        def current():
            if authorize is not None:
                authorize()
            if self.connectors.require_shopee_runtime(connector_id) != runtime:
                raise _changed()

        private = {
            value.get_secret_value()
            for value in (
                credentials.partner_key,
                credentials.access_token,
                credentials.refresh_token,
            )
            if value is not None
        }
        sources = []
        try:
            async with asyncio.timeout(30):
                current()
                if credentials.refresh_pending:
                    raise MarketplaceError(
                        "MARKETPLACE_REFRESH_UNCERTAIN",
                        "A Shopee token refresh is pending or uncertain. "
                        "Replace the credential pair.",
                        409,
                    )
                if credentials.access_token_expires_at <= self.clock() + 30:
                    if credentials.refresh_token is not None:
                        runtime, credentials = await self._refresh(
                            connector_id, runtime, credentials, private, current, authorize
                        )
                    if credentials.access_token_expires_at <= self.clock():
                        raise MarketplaceError(
                            "MARKETPLACE_TOKEN_EXPIRED",
                            "Replace the expired Shopee token pair.",
                            401,
                        )
                current()
                origin = runtime.connector.endpoint

                async def get(path, params=()):
                    payload = await self._request(
                        origin, path, params, credentials, private, current
                    )
                    sources.append(
                        {
                            "source_url": origin + path,
                            "request_id": _text(payload.get("request_id"), private, 160),
                        }
                    )
                    return payload

                store = await get(SHOPEE_SHOP_PATH)
                shop = {
                    # This endpoint does not echo shop_id; identity is the signed request's scope.
                    "id": credentials.shop_id,
                    "name": _text(store.get("shop_name"), private, 1000),
                    "region": _text(store.get("region"), private, 80),
                    "seller_type": "",
                    "status": _text(store.get("status"), private, 80),
                }
                if not shop["name"] or not shop["region"]:
                    raise _invalid()
                result = {"shops": [shop], "products": [], "next_page_token": None}
                partial = bool(store.get("warning"))
                if request.operation == "products":
                    statuses = (
                        SHOPEE_STATUSES
                        if request.product_status == "ALL"
                        else (request.product_status,)
                    )
                    page = await get(
                        SHOPEE_PRODUCTS_PATH,
                        [
                            ("offset", str(offset)),
                            ("page_size", str(request.page_size)),
                            *[("item_status", status) for status in statuses],
                        ],
                    )
                    data = _object(page.get("response"))
                    items = _array(data.get("item"), request.page_size)
                    total = _integer(data.get("total_count"), maximum=1_000_000_000)
                    has_next = data.get("has_next_page")
                    if type(has_next) is not bool or len(items) > total:
                        raise _invalid()
                    next_offset = None
                    if has_next:
                        next_offset = _integer(
                            data.get("next_offset"), maximum=999_999_999, minimum=offset + 1
                        )
                        if not items:
                            raise _invalid()
                    listed = {}
                    for item in items:
                        item = _object(item)
                        identifier = _id(item.get("item_id"))
                        if identifier in listed or item.get("item_status") not in statuses:
                            raise _invalid()
                        listed[identifier] = {
                            "id": identifier,
                            "title": "",
                            "status": item["item_status"],
                            "details_available": False,
                        }
                    identifiers = list(listed)
                    partial |= bool(page.get("warning"))
                    for start in range(0, len(identifiers), 50):
                        batch = identifiers[start : start + 50]
                        detail = await get(
                            SHOPEE_DETAILS_PATH,
                            [
                                (
                                    "item_id_list",
                                    json.dumps(
                                        [int(value) for value in batch], separators=(",", ":")
                                    ),
                                ),
                            ],
                        )
                        listed.update(self._details(detail, batch, private))
                        partial |= bool(detail.get("warning"))
                    partial |= any(not product["details_available"] for product in listed.values())
                    result = {
                        "shops": [],
                        "products": list(listed.values()),
                        "total_count": total,
                        "next_page_token": str(next_offset) if has_next else None,
                    }
                elif request.operation == "product_details":
                    detail = await get(
                        SHOPEE_DETAILS_PATH,
                        [
                            ("item_id_list", f"[{request.product_id}]"),
                        ],
                    )
                    products = self._details(detail, [request.product_id], private)
                    product = products.get(request.product_id)
                    if product is None:
                        raise MarketplaceError(
                            "MARKETPLACE_PRODUCT_UNAVAILABLE",
                            "This item has no available details in the authorized shop.",
                            404,
                        )
                    partial |= bool(detail.get("warning"))
                    if product["has_variants"]:
                        models = await get(SHOPEE_MODELS_PATH, [("item_id", request.product_id)])
                        product["variants"] = self._models(models, private)
                        partial |= bool(models.get("warning")) or not product["variants"]
                    else:
                        product["variants"] = []
                    total = len(product["variants"])
                    if offset > total:
                        raise MarketplaceError(
                            "MARKETPLACE_CURSOR_INVALID", "Read the first SKU page again."
                        )
                    product["variants"] = product["variants"][offset : offset + request.page_size]
                    product["variants_loaded"] = True
                    product["variants_offset"] = offset
                    product["variants_complete"] = offset == 0 and total <= request.page_size
                    result = {
                        "shops": [],
                        "products": [product],
                        "total_count": total,
                        "pagination_unit": "SKU",
                        "next_page_token": (
                            str(offset + request.page_size)
                            if offset + request.page_size < total
                            else None
                        ),
                    }
                current()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED",
                "The Shopee request failed. A pending refresh requires a new credential pair.",
                502,
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
            "data_scope": "authorized_seller_data",
        }

    @staticmethod
    def _details(payload, requested, private):
        result = {}
        for item in _array(_object(payload.get("response")).get("item_list"), len(requested)):
            item = _object(item)
            identifier = _id(item.get("item_id"))
            if (
                identifier not in requested
                or identifier in result
                or type(item.get("has_model")) is not bool
            ):
                raise _invalid()
            result[identifier] = {
                "id": identifier,
                "title": _text(item.get("item_name"), private),
                "status": _text(item.get("item_status"), private, 80) or "UNKNOWN",
                "sku": _text(item.get("item_sku"), private, 200),
                "category_id": _id(item["category_id"]) if item.get("category_id") else "",
                "has_variants": item["has_model"],
                "variants_loaded": False,
                "details_available": True,
                **_price_stock(item, private),
            }
        return result

    @staticmethod
    def _models(payload, private):
        data = _object(payload.get("response"))
        tiers = []
        for tier in _array(data.get("tier_variation"), 3):
            tier = _object(tier)
            tiers.append(
                (
                    _text(tier.get("name"), private, 100),
                    [
                        _text(_object(option).get("option"), private, 200)
                        for option in _array(tier.get("option_list"), 100)
                    ],
                )
            )
        variants, seen, combinations = [], set(), set()
        for model in _array(data.get("model"), 200):
            model = _object(model)
            identifier = _id(model.get("model_id"))
            indexes = _array(model.get("tier_index"), len(tiers))
            if len(indexes) != len(tiers) or identifier in seen:
                raise _invalid()
            options = []
            for (name, values), index in zip(tiers, indexes, strict=True):
                index = _integer(index, maximum=len(values) - 1)
                options.append({"name": name, "value": values[index]})
            if tuple(indexes) in combinations:
                raise _invalid()
            seen.add(identifier)
            combinations.add(tuple(indexes))
            variants.append(
                {
                    "id": identifier,
                    "title": " / ".join(item["value"] for item in options),
                    "options": options,
                    "sku": _text(model.get("model_sku"), private, 200),
                    "status": _text(model.get("model_status"), private, 80) or "UNKNOWN",
                    **_price_stock(model, private),
                }
            )
        return variants

    async def _refresh(self, connector_id, runtime, credentials, private, current, authorize):
        if credentials.refresh_token_expires_at <= self.clock():
            raise MarketplaceError(
                "MARKETPLACE_TOKEN_EXPIRED", "Replace the expired Shopee refresh token.", 401
            )
        current()
        pending = credentials.model_copy(update={"refresh_pending": True})
        try:
            claimed = self.connectors.replace_shopee_credentials(runtime, pending.vault_json())
        except CredentialChangedError:
            raise _changed() from None

        def claimed_current():
            if authorize is not None:
                authorize()
            if self.connectors.require_shopee_runtime(connector_id) != claimed:
                raise _changed()

        started = int(self.clock())
        payload = await self._request(
            runtime.connector.endpoint,
            SHOPEE_REFRESH_PATH,
            (),
            credentials,
            private,
            claimed_current,
            body={
                "partner_id": int(credentials.partner_id),
                "shop_id": int(credentials.shop_id),
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
        )
        if (
            _id(payload.get("partner_id")) != credentials.partner_id
            or _id(payload.get("shop_id")) != credentials.shop_id
            or any(
                payload.get(key) is not None
                for key in ("merchant_id", "supplier_id", "user_id", "principal_id")
            )
        ):
            raise _invalid()
        ttl = _integer(payload.get("expire_in"), minimum=1, maximum=14400)
        values = json.loads(credentials.vault_json())
        values.update(
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
            access_token_expires_at=started + ttl,
            refresh_token_expires_at=started + 30 * 86400,
            refresh_pending=False,
        )
        try:
            updated = parse_shopee_credentials(json.dumps(values))
        except ValueError:
            raise _invalid() from None
        if updated.refresh_token is None or updated.refresh_token == credentials.refresh_token:
            raise _invalid()
        private.update(
            {updated.access_token.get_secret_value(), updated.refresh_token.get_secret_value()}
        )
        claimed_current()
        try:
            saved = self.connectors.replace_shopee_credentials(claimed, updated.vault_json())
        except CredentialChangedError:
            raise _changed() from None
        return saved, updated

    async def _request(self, origin, path, params, credentials, private, current, *, body=None):
        destination = await pin_marketplace_destination(origin + path)
        current()
        timestamp = str(int(self.clock()))
        signature = sign_shopee_request(
            credentials.partner_key.get_secret_value(),
            credentials.partner_id,
            path,
            timestamp,
            credentials.access_token.get_secret_value() if body is None else "",
            credentials.shop_id if body is None else "",
        )
        private.add(signature)
        query = [
            ("partner_id", credentials.partner_id),
            ("timestamp", timestamp),
            ("sign", signature),
        ]
        if body is None:
            query.extend(
                [
                    ("access_token", credentials.access_token.get_secret_value()),
                    ("shop_id", credentials.shop_id),
                ]
            )
        query.extend(params)
        request = httpx.Request(
            "GET" if body is None else "POST",
            destination.request_url,
            params=query,
            content=None if body is None else json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Host": destination.host_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
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
                "MARKETPLACE_HTTP_REJECTED", f"Shopee rejected the request (HTTP {status}).", 502
            ),
        )
        current()
        try:
            parse_json(content)  # Reject duplicate keys before preserving decimal number lexemes.
            payload = json.loads(content, parse_float=Decimal)
        except (ValueError, RecursionError, InvalidOperation):
            raise _invalid() from None
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), str):
            raise _invalid()
        if payload["error"]:
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                "Shopee rejected the request. Check seller authorization.",
                502,
            )
        if not _text(payload.get("request_id"), private, 160):
            raise _invalid()
        return payload
