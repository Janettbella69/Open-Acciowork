"""Seller-bound AliExpress POP reads using the current TOP migration contract."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

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
from .untrusted import UNTRUSTED_LIMITS, UntrustedSource, sanitize_untrusted_text

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

ALIEXPRESS_TYPE = "aliexpress"
ALIEXPRESS_ORIGIN = "https://api-sg.aliexpress.com"
ALIEXPRESS_PATH = "/sync"
ALIEXPRESS_PROFILE_API = "aliexpress.merchant.profile.get"
ALIEXPRESS_PRODUCTS_API = "aliexpress.postproduct.redefining.findproductinfolistquery"
ALIEXPRESS_DETAILS_API = "aliexpress.offer.product.query"
ALIEXPRESS_STORE_SCOPE = "aliexpress:seller:read"
ALIEXPRESS_PRODUCT_SCOPE = "aliexpress:products:read"
ALIEXPRESS_DEFAULT_SCOPE = f"{ALIEXPRESS_STORE_SCOPE} {ALIEXPRESS_PRODUCT_SCOPE}"
ALIEXPRESS_STATUSES = frozenset(
    {
        "onSelling",
        "offline",
        "auditing",
        "editingRequired",
        "service_delete",
        "deleted",
    }
)
MAX_ALIEXPRESS_PAGE_SIZE = 50  # Upstream requires less than 100; observations remain bounded.
MAX_ALIEXPRESS_SKUS = 256
# Reserve room for the parent product, request sources and native MCP receipt envelope.
MAX_ALIEXPRESS_SKU_PAGE_CHARS = (
    UNTRUSTED_LIMITS[UntrustedSource.MARKETPLACE_PAGE].total_chars - 12_000
)


class AliExpressCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    app_key: SecretStr = Field(min_length=1, max_length=160)
    app_secret: SecretStr = Field(min_length=1, max_length=512)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    seller_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    access_token_expires_at: int = Field(gt=0, le=100_000_000_000)

    @model_validator(mode="after")
    def credential_encoding(self):
        if int(self.seller_id) >= 2**63:
            raise ValueError("invalid seller id")
        for value in (self.app_key, self.app_secret, self.access_token):
            if not re.fullmatch(r"[A-Za-z0-9_.~:+/=-]+", value.get_secret_value()):
                raise ValueError("invalid credential encoding")
        return self


def parse_aliexpress_credentials(raw):
    try:
        if len(raw.encode()) > 8000:
            raise ValueError
        return AliExpressCredentials.model_validate(parse_json(raw.encode()))
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            "AliExpress requires app credentials, seller token, seller ID and token expiry"
        ) from None


def sign_aliexpress_request(parameters, app_secret):
    """TOP migration protocol: method is sorted with parameters, never prefixed."""
    value = "".join(key + parameters[key] for key in sorted(parameters) if key != "sign")
    return hmac.new(app_secret.encode(), value.encode(), hashlib.sha256).hexdigest().upper()


def _invalid():
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID",
        "AliExpress returned an invalid or oversized response.",
        502,
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
    # Current examples encode Number and Boolean fields as strings.
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _boolean(value):
    if type(value) is bool:
        return value
    if value in ("true", "false"):
        return value == "true"
    raise _invalid()


def _id(value):
    return str(_integer(value, minimum=1, maximum=2**63 - 1))


def _text(value, private, maximum=1000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise _invalid()
    pattern = "|".join(re.escape(secret) for secret in sorted(private, key=len, reverse=True))
    value = re.sub(pattern, "[redacted]", value)
    result = sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD)
    if result.truncated:
        raise _invalid()
    return result.text


def _amount(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,8})?", value):
        raise _invalid()
    return value


def _currency(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}(?:;[A-Z]{3}){0,9}", value):
        raise _invalid()
    # A list such as USD;RUB cannot be assigned to one monetary amount.
    return value if ";" not in value else None


def _quote(amount, currency):
    value = _amount(amount)
    return None if value is None else {"amount": value, "currency": _currency(currency)}


class AliExpressReader:
    def __init__(self, connectors: ConnectorRegistry, *, transport_factory=None, clock=time.time):
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock

    def identity_parts(self, connector_id):
        runtime = self.connectors.require_aliexpress_runtime(connector_id)
        credentials = parse_aliexpress_credentials(runtime.credential.get_secret_value())
        return [
            ALIEXPRESS_TYPE,
            runtime.connector.endpoint,
            credentials.app_key.get_secret_value(),
            credentials.seller_id,
        ]

    def require_grantable(self, connector_id):
        runtime = self.connectors.require_aliexpress_runtime(connector_id)
        if not {ALIEXPRESS_STORE_SCOPE, ALIEXPRESS_PRODUCT_SCOPE}.issubset(
            runtime.connector.scope.split(),
        ):
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED",
                "Allow AliExpress product reads first.",
            )

    async def read(self, connector_id, request: MarketplaceReadRequest, *, authorize=None):
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_aliexpress_runtime(connector_id)
        credentials = parse_aliexpress_credentials(runtime.credential.get_secret_value())
        if request.operation not in {"authorized_shops", "products", "product_details"}:
            raise MarketplaceError(
                "MARKETPLACE_OPERATION_UNSUPPORTED",
                "This AliExpress read is not available.",
            )
        if request.operation == "products" and request.product_status not in ALIEXPRESS_STATUSES:
            raise MarketplaceError(
                "MARKETPLACE_FILTER_REQUIRED",
                "Select onSelling, offline, auditing, editingRequired, service_delete or deleted.",
            )
        if request.operation != "products" and request.product_status != "ALL":
            raise MarketplaceError("MARKETPLACE_FILTER_UNSUPPORTED", "This read takes no filter.")
        if request.page_size > MAX_ALIEXPRESS_PAGE_SIZE:
            raise MarketplaceError("MARKETPLACE_PAGE_SIZE_INVALID", "Use at most 50 records.")
        required = {ALIEXPRESS_STORE_SCOPE}
        if request.operation != "authorized_shops":
            required.add(ALIEXPRESS_PRODUCT_SCOPE)
            if request.shop_id != credentials.seller_id:
                raise MarketplaceError(
                    "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                    "Select the credential's seller ID.",
                    403,
                )
        if not required.issubset(runtime.connector.scope.split()):
            raise MarketplaceError("MARKETPLACE_SCOPE_REQUIRED", "Allow the required seller reads.")
        if request.product_id and (
            not re.fullmatch(r"[1-9][0-9]{0,18}", request.product_id)
            or int(request.product_id) >= 2**63
        ):
            raise MarketplaceError(
                "MARKETPLACE_PRODUCT_INVALID", "Use a valid AliExpress product ID."
            )
        page, offset, expected_digest = 1, 0, None
        if request.page_token is not None:
            if request.operation == "products" and re.fullmatch(
                r"[1-9][0-9]{0,5}", request.page_token
            ):
                page = int(request.page_token)
            elif request.operation == "product_details" and re.fullmatch(
                r"[1-9][0-9]{0,2}\.[a-f0-9]{64}",
                request.page_token,
            ):
                number, expected_digest = request.page_token.split(".")
                offset = int(number)
                if offset >= MAX_ALIEXPRESS_SKUS:
                    raise MarketplaceError(
                        "MARKETPLACE_CURSOR_INVALID", "Use the returned SKU cursor."
                    )
            else:
                raise MarketplaceError("MARKETPLACE_CURSOR_INVALID", "Use the returned page token.")
        private = {
            value.get_secret_value()
            for value in (
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
            )
        }

        def current():
            if authorize is not None:
                authorize()
            if self.connectors.require_aliexpress_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "AliExpress credentials changed. Read again.",
                    409,
                )
            if credentials.access_token_expires_at <= self.clock():
                raise MarketplaceError(
                    "MARKETPLACE_TOKEN_EXPIRED",
                    "Replace the expired AliExpress seller token.",
                    401,
                )

        sources = []

        async def get(api, parameters):
            current()
            payload = await self._request(api, parameters, credentials, private, current)
            sources.append(
                {
                    "source_url": ALIEXPRESS_ORIGIN + ALIEXPRESS_PATH,
                    "api_name": api,
                    "request_id": _text(payload.get("request_id"), private, 160),
                }
            )
            return payload

        async def products(status, number, count, product_id=None):
            query = {"product_status_type": status, "current_page": number, "page_size": count}
            if product_id is not None:
                query["product_id"] = int(product_id)
            payload = await get(
                ALIEXPRESS_PRODUCTS_API,
                {
                    "aeop_a_e_product_list_query": json.dumps(query, separators=(",", ":")),
                },
            )
            data = _object(payload.get("result"))
            if not _boolean(data.get("success")) or (
                data.get("error_code") is not None and _integer(data["error_code"]) != 0
            ):
                raise MarketplaceError(
                    "MARKETPLACE_API_REJECTED",
                    "AliExpress rejected the product read.",
                    502,
                )
            current_page = _integer(data.get("current_page"), minimum=1)
            total = _integer(data.get("product_count"))
            total_pages = _integer(data.get("total_page"), maximum=999999)
            items = _array(data.get("aeop_a_e_product_display_d_t_o_list", []), count)
            if (
                current_page != number
                or len(items) > total
                or (items and current_page > total_pages)
                or (total_pages > current_page and not items)
            ):
                raise _invalid()
            normalized = [self._product(_object(item), status, private) for item in items]
            if len({item["id"] for item in normalized}) != len(normalized):
                raise _invalid()
            return normalized, total, total_pages

        try:
            async with asyncio.timeout(30):
                profile = _object((await get(ALIEXPRESS_PROFILE_API, {})).get("profile"))
                seller_id = _id(profile.get("seller_id"))
                if seller_id != credentials.seller_id:
                    raise MarketplaceError(
                        "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                        "The token belongs to another seller.",
                        403,
                    )
                result = {
                    "shops": [
                        {
                            "id": seller_id,
                            "name": "",
                            "identity_kind": "seller_account",
                            "region": _text(profile.get("country"), private, 80),
                            "seller_type": "",
                            "currency": _currency(profile.get("quotation_currency")),
                        }
                    ],
                    "products": [],
                    "next_page_token": None,
                    "authorization_expires_at": datetime.fromtimestamp(
                        credentials.access_token_expires_at,
                        UTC,
                    ).isoformat(),
                }
                partial = False
                if request.operation == "products":
                    items, total, total_pages = await products(
                        request.product_status,
                        page,
                        request.page_size,
                    )
                    partial = page == 1 and total_pages <= 1 and len(items) != total
                    result = {
                        "shops": [],
                        "products": items,
                        "total_count": total,
                        "page_number": page,
                        "total_pages": total_pages,
                        "product_status_filter": request.product_status,
                        "next_page_token": str(page + 1) if page < total_pages else None,
                    }
                elif request.operation == "product_details":
                    data = _object(
                        (
                            await get(
                                ALIEXPRESS_DETAILS_API,
                                {
                                    "product_id": request.product_id,
                                },
                            )
                        ).get("result")
                    )
                    if _id(data.get("product_id")) != request.product_id:
                        raise _invalid()
                    status = data.get("product_status_type")
                    if status not in ALIEXPRESS_STATUSES:
                        raise _invalid()
                    # Verify membership through the token's seller-scoped catalog.
                    owned, total, total_pages = await products(status, 1, 1, request.product_id)
                    if not owned:
                        raise MarketplaceError(
                            "MARKETPLACE_PRODUCT_UNAVAILABLE",
                            "This product is not in the seller token's catalog.",
                            404,
                        )
                    if (
                        len(owned) != 1
                        or owned[0]["id"] != request.product_id
                        or total != 1
                        or total_pages != 1
                    ):
                        raise _invalid()
                    product = owned[0]
                    if product["product_type"] != "POP":
                        raise MarketplaceError(
                            "MARKETPLACE_PRODUCT_TYPE_UNSUPPORTED",
                            "SKU details currently require a standard POP product.",
                        )
                    skus = _array(data.get("aeop_ae_product_s_k_us", []), MAX_ALIEXPRESS_SKUS)
                    variants = [self._sku(_object(item), private) for item in skus]
                    if len({item["id"] for item in variants}) != len(variants):
                        raise _invalid()
                    variants.sort(key=lambda item: item["id"])
                    product.update(
                        variants=variants,
                        variants_loaded=True,
                        listed_price=_quote(data.get("product_price"), data.get("currency_code")),
                        has_country_pricing=bool(data.get("aeop_national_quote_configuration")),
                    )
                    digest = hashlib.sha256(
                        json.dumps(
                            product,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if expected_digest is not None and expected_digest != digest:
                        raise MarketplaceError(
                            "MARKETPLACE_PAGINATION_CHANGED",
                            "The product or SKUs changed. Read SKU details from the first page.",
                            409,
                        )
                    if offset and offset >= len(variants):
                        raise MarketplaceError(
                            "MARKETPLACE_CURSOR_INVALID", "Use the returned SKU cursor."
                        )
                    end, page_chars = offset, 2
                    for variant in variants[offset : offset + request.page_size]:
                        variant_chars = len(json.dumps(variant, ensure_ascii=False)) + 2
                        if page_chars + variant_chars > MAX_ALIEXPRESS_SKU_PAGE_CHARS:
                            break
                        page_chars += variant_chars
                        end += 1
                    if variants and end == offset:
                        raise _invalid()
                    product.update(
                        variants=variants[offset:end],
                        variants_offset=offset,
                        variants_complete=offset == 0 and end >= len(variants),
                    )
                    partial = not variants
                    result = {
                        "shops": [],
                        "products": [product],
                        "pagination_unit": "SKU",
                        "total_count": len(variants),
                        "next_page_token": f"{end}.{digest}" if end < len(variants) else None,
                    }
                current()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED",
                "The AliExpress read failed. Try reading again.",
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
    def _product(item, status, private):
        product_type = _text(item.get("product_type"), private, 80)
        return {
            "id": _id(item.get("product_id")),
            "title": _text(item.get("subject"), private),
            "status": status,
            "status_source": "requested_filter",
            "product_type": product_type,
            "details_available": product_type == "POP",
            "variants_loaded": False,
            "price_range": {
                "currency": _currency(item.get("currency_code")),
                "min": _amount(item.get("product_min_price")),
                "max": _amount(item.get("product_max_price")),
            },
            "updated_at": _text(item.get("gmt_modified"), private, 80),
        }

    @staticmethod
    def _sku(item, private):
        platform_id = item.get("sku_Id")
        signature = _text(item.get("id"), private, 300)
        if platform_id is not None:
            identifier, kind = _id(platform_id), "platform"
        elif signature and re.fullmatch(r"[0-9:;_-]{1,300}", signature):
            identifier, kind = signature, "properties"
        else:
            raise _invalid()
        attributes = []
        for option in _array(item.get("aeop_s_k_u_property_list", []), 3):
            option = _object(option)
            attributes.append(
                {
                    "property_id": _id(option.get("sku_property_id")),
                    "value_id": _id(option["property_value_id"])
                    if option.get("property_value_id") is not None
                    else None,
                    "value": _text(option.get("property_value_definition_name"), private, 150)
                    or _text(option.get("sku_property_value"), private, 150),
                }
            )
        countries = []
        for price in _array(item.get("aeop_s_k_u_national_discount_price_list", []), 40):
            price = _object(price)
            country = price.get("shipto_country")
            if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
                raise _invalid()
            quote = _quote(price.get("discount_price"), item.get("currency_code"))
            countries.append({"country": country, "quote": quote})
        if len({price["country"] for price in countries}) != len(countries):
            raise _invalid()
        quantity, available = item.get("ipm_sku_stock"), item.get("sku_stock")
        return {
            "id": identifier,
            "id_kind": kind,
            "property_signature": signature,
            "title": " / ".join(option["value"] for option in attributes),
            "status": "UNKNOWN",
            "sku": _text(item.get("sku_code"), private, 200),
            "attributes": attributes,
            "listed_price": _quote(item.get("sku_price"), item.get("currency_code")),
            "daily_discount_price": _quote(
                item.get("sku_discount_price"), item.get("currency_code")
            ),
            "country_discount_prices": countries,
            "stock": {
                "total_available_stock": None if quantity is None else _integer(quantity),
                "total_reserved_stock": None,
            },
            "in_stock": None if available is None else _boolean(available),
        }

    async def _request(self, api, parameters, credentials, private, current):
        destination = await pin_marketplace_destination(ALIEXPRESS_ORIGIN + ALIEXPRESS_PATH)
        current()
        body = {
            "method": api,
            "app_key": credentials.app_key.get_secret_value(),
            "access_token": credentials.access_token.get_secret_value(),
            "timestamp": str(int(self.clock() * 1000)),
            "sign_method": "sha256",
            "simplify": "true",
            **parameters,
        }
        signature = sign_aliexpress_request(body, credentials.app_secret.get_secret_value())
        private.add(signature)
        body["sign"] = signature
        request = httpx.Request(
            "POST",
            destination.request_url,
            content=urlencode(body).encode(),
            headers={
                "Host": destination.host_header,
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
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
                "MARKETPLACE_HTTP_REJECTED",
                f"AliExpress rejected the read (HTTP {status}).",
                502,
            ),
        )
        current()
        try:
            payload = _object(parse_json(content))
        except (ValueError, RecursionError):
            raise _invalid() from None
        code = payload.get("code")
        if "error_response" not in payload and type(code) not in {str, int}:
            raise _invalid()
        if "error_response" in payload or str(code) != "0":
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                "AliExpress rejected the read. Check seller authorization.",
                502,
            )
        return payload
