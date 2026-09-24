"""Bounded Shopify Admin reader and human-managed order webhook adapter.

A Dev Dashboard app's client ID and secret stay in the connector vault. The reader
exchanges them through the client credentials grant for a 24-hour access token held
only in process memory, then runs fixed GraphQL Admin queries. The reader never
requests customer names, emails, addresses, or phone numbers. Fixed webhook
subscription writes are available only to project administrators, not Agent tools.
See docs/marketplace-connectors.md for source versions and acceptance limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from .marketplaces import (
    SHOPIFY_PRODUCT_STATUSES,
    MarketplaceError,
    MarketplaceReadRequest,
    default_marketplace_transport,
    pin_marketplace_destination,
    send_marketplace_request,
)
from .models import utc_now
from .untrusted import UntrustedSource, sanitize_untrusted_text

if TYPE_CHECKING:
    from .connectors import CommercialConnectorRuntime, ConnectorRegistry

SHOPIFY_API_VERSION = "2026-07"
SHOPIFY_TOKEN_PATH = "/admin/oauth/access_token"
SHOPIFY_GRAPHQL_PATH = f"/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
SHOPIFY_PRODUCT_SCOPE = "read_products"
SHOPIFY_ORDER_SCOPE = "read_orders"
SHOPIFY_ALL_ORDERS_SCOPE = "read_all_orders"
SHOPIFY_DEFAULT_SCOPE = f"{SHOPIFY_PRODUCT_SCOPE} {SHOPIFY_ORDER_SCOPE}"
SHOPIFY_ORDER_WEBHOOK_TOPIC = "ORDERS_CREATE"
SHOPIFY_ORDER_WEBHOOK_FIELDS = (
    "id", "name", "created_at", "financial_status", "fulfillment_status",
    "total_price", "currency",
)
# Shopify's example refreshes a minute early; a token is never used closer to expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 60

ORDER_FINANCIAL_STATUSES = frozenset({
    "AUTHORIZED", "EXPIRED", "PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED", "PENDING",
    "REFUNDED", "VOIDED",
})
ORDER_FULFILLMENT_STATUSES = frozenset({
    "FULFILLED", "IN_PROGRESS", "ON_HOLD", "OPEN", "PARTIALLY_FULFILLED", "PENDING_FULFILLMENT",
    "REQUEST_DECLINED", "RESTOCKED", "SCHEDULED", "UNFULFILLED",
})

_STORE_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com")
_HEADER_SAFE = re.compile(r"[A-Za-z0-9_.~:+/=-]+")
_SCOPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_MONEY = re.compile(r"-?\d{1,18}(?:\.\d{1,10})?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,160}")

# Fixed documents: callers only choose page size, cursor and a documented status.
SHOP_QUERY = (
    "query OphiraShop { shop { id name myshopifyDomain currencyCode "
    "shopAddress { countryCodeV2 } } }"
)
PRODUCTS_QUERY = (
    "query OphiraProducts($first: Int!, $after: String, $query: String) { shop { id } "
    "products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT, reverse: true) "
    "{ nodes { id title status vendor productType updatedAt } "
    "pageInfo { hasNextPage endCursor } } }"
)
ORDERS_QUERY = (
    "query OphiraOrders($first: Int!, $after: String) { shop { id } "
    "orders(first: $first, after: $after, sortKey: CREATED_AT, reverse: true) "
    "{ nodes { id name createdAt cancelledAt test displayFinancialStatus "
    "displayFulfillmentStatus currentSubtotalLineItemsQuantity "
    "currentTotalPriceSet { shopMoney { amount currencyCode } } } "
    "pageInfo { hasNextPage endCursor } } }"
)
ORDER_WEBHOOK_LOOKUP = (
    "query OphiraOrderWebhook($uri: String!) { "
    "webhookSubscriptions(first: 3, topics: [ORDERS_CREATE], uri: $uri) "
    "{ nodes { id topic uri includeFields } pageInfo { hasNextPage } } }"
)
ORDER_WEBHOOK_CREATE = (
    "mutation OphiraOrderWebhookCreate($webhookSubscription: WebhookSubscriptionInput!) { "
    "webhookSubscriptionCreate(topic: ORDERS_CREATE, webhookSubscription: $webhookSubscription) "
    "{ webhookSubscription { id topic uri includeFields } "
    "userErrors { field message } } }"
)
ORDER_WEBHOOK_DELETE = (
    "mutation OphiraOrderWebhookDelete($id: ID!) { "
    "webhookSubscriptionDelete(id: $id) "
    "{ deletedWebhookSubscriptionId userErrors { field message } } }"
)
_WEBHOOK_SUBSCRIPTION_ID = re.compile(r"gid://shopify/WebhookSubscription/[1-9][0-9]{0,19}")


def check_shopify_store_endpoint(endpoint: str | None) -> str | None:
    """Accept only an exact lowercase ``https://<store>.myshopify.com`` origin."""
    if not endpoint or not _STORE_ORIGIN.fullmatch(endpoint):
        return "Shopify requires the store's https://<store>.myshopify.com address"
    return None


class ShopifyCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    client_id: SecretStr = Field(min_length=16, max_length=255)
    client_secret: SecretStr = Field(min_length=16, max_length=512)

    @model_validator(mode="after")
    def safe_values(self) -> ShopifyCredentials:
        for value in (self.client_id, self.client_secret):
            if not _HEADER_SAFE.fullmatch(value.get_secret_value()):
                raise ValueError("invalid credential encoding")
        return self


def parse_shopify_credentials(raw: str) -> ShopifyCredentials:
    try:
        if len(raw) > 8000:
            raise ValueError("too large")
        return ShopifyCredentials.model_validate_json(raw)
    except (ValidationError, ValueError):
        # Pydantic errors include input_value, so never relay their text.
        raise ValueError(
            "Shopify requires a Dev Dashboard client ID and client secret"
        ) from None


@dataclass(frozen=True)
class _AccessToken:
    key: str
    value: str = field(repr=False)
    scopes: frozenset[str] = frozenset()
    expires_at: float = 0.0


def _invalid() -> MarketplaceError:
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID", "The marketplace returned an invalid response.", 502,
    )


def _rate_limited() -> MarketplaceError:
    return MarketplaceError(
        "MARKETPLACE_RATE_LIMITED", "Shopify rate limited the read. Wait a moment and read again.",
        429,
    )


def _token_rejected(status: int) -> MarketplaceError:
    if status == 429:
        return _rate_limited()
    return MarketplaceError(
        "MARKETPLACE_CREDENTIALS_REJECTED",
        f"Shopify refused the client credentials (HTTP {status}). Confirm the client ID and "
        "secret, that the app is installed on this store, and that the app and store belong "
        "to the same Dev Dashboard organization.", 502,
    )


def _read_rejected(status: int) -> MarketplaceError:
    if status == 402:
        return MarketplaceError(
            "MARKETPLACE_SHOP_UNAVAILABLE",
            "Shopify blocked API access for this store (HTTP 402). Check its plan and billing.",
            502,
        )
    if status == 403:
        return MarketplaceError(
            "MARKETPLACE_SCOPE_REQUIRED",
            "Shopify refused this read (HTTP 403). Check the app's granted scopes.", 403,
        )
    if status == 404:
        return MarketplaceError(
            "MARKETPLACE_SHOP_NOT_FOUND", "Shopify could not find this store address (HTTP 404).",
            502,
        )
    if status == 423:
        return MarketplaceError(
            "MARKETPLACE_SHOP_UNAVAILABLE", "Shopify reports this store is locked (HTTP 423).", 502,
        )
    if status == 429:
        return _rate_limited()
    return MarketplaceError(
        "MARKETPLACE_HTTP_REJECTED", f"The marketplace rejected the read (HTTP {status}).", 502,
    )


class ShopifyReader:
    def __init__(
        self, connectors: ConnectorRegistry, *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock
        self._tokens: dict[str, _AccessToken] = {}

    def identity_parts(self, connector_id: str) -> list[str | None]:
        runtime = self.connectors.require_shopify_runtime(connector_id)
        credentials = parse_shopify_credentials(runtime.credential.get_secret_value())
        return [runtime.connector.type, runtime.connector.endpoint,
                credentials.client_id.get_secret_value()]

    def require_grantable(self, connector_id: str) -> None:
        runtime = self.connectors.require_shopify_runtime(connector_id)
        if SHOPIFY_PRODUCT_SCOPE not in runtime.connector.scope.split():
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Allow Shopify product reads first.",
            )

    async def read(
        self, connector_id: str, request: MarketplaceReadRequest, *,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if authorize is not None:
            authorize()
        runtime = self.connectors.require_shopify_runtime(connector_id)
        credentials = parse_shopify_credentials(runtime.credential.get_secret_value())
        if request.product_status not in SHOPIFY_PRODUCT_STATUSES:
            raise MarketplaceError(
                "MARKETPLACE_FILTER_UNSUPPORTED",
                "Use a Shopify product status filter: ACTIVE, DRAFT, ARCHIVED or UNLISTED.",
            )
        required = {"products": SHOPIFY_PRODUCT_SCOPE, "orders": SHOPIFY_ORDER_SCOPE}.get(
            request.operation,
        )
        if required and required not in runtime.connector.scope.split():
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED",
                "This Shopify read is not allowed in the connector settings.",
            )
        origin = runtime.connector.endpoint
        assert origin is not None
        private_values = {
            credentials.client_id.get_secret_value(), credentials.client_secret.get_secret_value(),
        }

        def check_current() -> None:
            if authorize is not None:
                authorize()
            if self.connectors.require_shopify_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "The connector changed during the request. Read again with current settings.",
                    409,
                )

        try:
            async with asyncio.timeout(30):
                check_current()
                token = await self._access_token(runtime, credentials, check_current)
                private_values.add(token.value)
                if required and required not in token.scopes:
                    raise MarketplaceError(
                        "MARKETPLACE_SCOPE_REQUIRED",
                        f"The Shopify app is not granted {required}. Add it to a released app "
                        "version and approve the change on the store.", 403,
                    )
                result = await self._operation(
                    origin, token, request, check_current, private_values,
                )
                check_current()
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED", "The marketplace request failed. Try reading again.",
                502,
            ) from None
        return {
            "connector_id": connector_id, "operation": request.operation,
            "shop_id": request.shop_id, **result,
            "source_url": origin + SHOPIFY_GRAPHQL_PATH,
            "retrieved_at": utc_now().isoformat(),
            "data_scope": "authorized_seller_data",
        }

    async def _order_webhook_graphql(
        self, connector_id: str, query: str, variables: dict[str, Any], *,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        """Fixed, administrator-only webhook operation; never exposed as an Agent read."""
        authorize()
        runtime = self.connectors.require_shopify_runtime(connector_id)
        if SHOPIFY_ORDER_SCOPE not in runtime.connector.scope.split():
            raise MarketplaceError(
                "MARKETPLACE_SCOPE_REQUIRED", "Allow Shopify order reads first.", 403,
            )
        credentials = parse_shopify_credentials(runtime.credential.get_secret_value())
        origin = runtime.connector.endpoint
        assert origin is not None
        private_values = {
            credentials.client_id.get_secret_value(), credentials.client_secret.get_secret_value(),
        }

        def check_current() -> None:
            authorize()
            if self.connectors.require_shopify_runtime(connector_id) != runtime:
                raise MarketplaceError(
                    "MARKETPLACE_CONFIGURATION_CHANGED",
                    "The Shopify connector changed during subscription setup.", 409,
                )

        try:
            async with asyncio.timeout(30):
                check_current()
                token = await self._access_token(runtime, credentials, check_current)
                private_values.add(token.value)
                if SHOPIFY_ORDER_SCOPE not in token.scopes:
                    raise MarketplaceError(
                        "MARKETPLACE_SCOPE_REQUIRED",
                        "The Shopify app is not granted read_orders.", 403,
                    )
                data, _request_id = await self._graphql(
                    origin, token, query, variables, check_current, private_values,
                )
                check_current()
                return data
        except (TimeoutError, httpx.HTTPError, OSError):
            raise MarketplaceError(
                "MARKETPLACE_NETWORK_FAILED",
                "Shopify subscription status is uncertain. Check it before retrying.", 502,
            ) from None

    @staticmethod
    def _webhook_node(value: Any, uri: str, *, require_fields: bool = True) -> str:
        if not isinstance(value, dict):
            raise _invalid()
        subscription_id = value.get("id")
        fields = value.get("includeFields")
        if (
            not isinstance(subscription_id, str)
            or not _WEBHOOK_SUBSCRIPTION_ID.fullmatch(subscription_id)
            or value.get("topic") != SHOPIFY_ORDER_WEBHOOK_TOPIC
            or value.get("uri") != uri
            or (require_fields and (
                not isinstance(fields, list)
                or not all(isinstance(field, str) for field in fields)
                or set(fields) != set(SHOPIFY_ORDER_WEBHOOK_FIELDS)
                or len(fields) != len(SHOPIFY_ORDER_WEBHOOK_FIELDS)
            ))
        ):
            raise MarketplaceError(
                "MARKETPLACE_WEBHOOK_CONFLICT",
                "A different Shopify subscription already uses this delivery URL.", 409,
            )
        return subscription_id

    async def find_order_webhook_subscription(
        self, connector_id: str, uri: str, *, authorize: Callable[[], None],
        require_fields: bool = True,
    ) -> str | None:
        data = await self._order_webhook_graphql(
            connector_id, ORDER_WEBHOOK_LOOKUP, {"uri": uri}, authorize=authorize,
        )
        connection = data.get("webhookSubscriptions")
        if not isinstance(connection, dict):
            raise _invalid()
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if (
            not isinstance(nodes, list) or len(nodes) > 3
            or not isinstance(page_info, dict)
            or type(page_info.get("hasNextPage")) is not bool
        ):
            raise _invalid()
        if page_info["hasNextPage"] or len(nodes) > 1:
            raise MarketplaceError(
                "MARKETPLACE_WEBHOOK_CONFLICT",
                "Multiple Shopify subscriptions use this delivery URL.", 409,
            )
        return self._webhook_node(nodes[0], uri, require_fields=require_fields) if nodes else None

    async def create_order_webhook_subscription(
        self, connector_id: str, uri: str, *, authorize: Callable[[], None],
    ) -> str:
        data = await self._order_webhook_graphql(
            connector_id, ORDER_WEBHOOK_CREATE,
            {"webhookSubscription": {
                "uri": uri, "includeFields": list(SHOPIFY_ORDER_WEBHOOK_FIELDS),
            }},
            authorize=authorize,
        )
        result = data.get("webhookSubscriptionCreate")
        if not isinstance(result, dict) or not isinstance(result.get("userErrors"), list):
            raise _invalid()
        if result["userErrors"]:
            raise MarketplaceError(
                "MARKETPLACE_WEBHOOK_REJECTED",
                "Shopify rejected the order webhook. Check app scopes and public HTTPS setup.",
                409,
            )
        return self._webhook_node(result.get("webhookSubscription"), uri)

    async def delete_order_webhook_subscription(
        self, connector_id: str, subscription_id: str, *,
        authorize: Callable[[], None],
    ) -> None:
        if not _WEBHOOK_SUBSCRIPTION_ID.fullmatch(subscription_id):
            raise MarketplaceError(
                "MARKETPLACE_WEBHOOK_CONFLICT", "Invalid Shopify subscription ID.", 409,
            )
        data = await self._order_webhook_graphql(
            connector_id, ORDER_WEBHOOK_DELETE, {"id": subscription_id},
            authorize=authorize,
        )
        result = data.get("webhookSubscriptionDelete")
        if not isinstance(result, dict) or not isinstance(result.get("userErrors"), list):
            raise _invalid()
        if result["userErrors"]:
            raise MarketplaceError(
                "MARKETPLACE_WEBHOOK_REJECTED",
                "Shopify rejected removal of the order webhook.", 409,
            )
        if result.get("deletedWebhookSubscriptionId") != subscription_id:
            raise _invalid()

    async def _operation(
        self, origin: str, token: _AccessToken, request: MarketplaceReadRequest,
        check_current: Callable[[], None], private_values: set[str],
    ) -> dict[str, Any]:
        if request.operation == "authorized_shops":
            data, request_id = await self._graphql(
                origin, token, SHOP_QUERY, {}, check_current, private_values,
            )
            return {
                "shops": [self._shop(data.get("shop"), origin, private_values)],
                "products": [], "orders": [], "next_page_token": None,
                "granted_scopes": sorted(token.scopes), "request_id": request_id,
            }
        if request.operation == "products":
            status = request.product_status
            variables = {
                "first": request.page_size, "after": request.page_token,
                "query": None if status == "ALL" else f"status:{status.lower()}",
            }
            data, request_id = await self._graphql(
                origin, token, PRODUCTS_QUERY, variables, check_current, private_values,
            )
            self._require_shop(data, request.shop_id)
            nodes, cursor = self._page(data.get("products"), request.page_size, private_values)
            products = [self._product(node, private_values) for node in nodes]
            self._require_unique(products)
            return {
                "shops": [], "products": products, "orders": [], "next_page_token": cursor,
                "request_id": request_id,
            }
        data, request_id = await self._graphql(
            origin, token, ORDERS_QUERY,
            {"first": request.page_size, "after": request.page_token},
            check_current, private_values,
        )
        self._require_shop(data, request.shop_id)
        nodes, cursor = self._page(data.get("orders"), request.page_size, private_values)
        orders = [self._order(node, private_values) for node in nodes]
        self._require_unique(orders)
        return {
            "shops": [], "products": [], "orders": orders, "next_page_token": cursor,
            # Without read_all_orders Shopify returns only the last 60 days of orders.
            "order_window": (
                "all" if SHOPIFY_ALL_ORDERS_SCOPE in token.scopes else "last_60_days"
            ),
            "request_id": request_id,
        }

    async def _access_token(
        self, runtime: CommercialConnectorRuntime, credentials: ShopifyCredentials,
        check_current: Callable[[], None],
    ) -> _AccessToken:
        origin = runtime.connector.endpoint
        assert origin is not None
        client_id = credentials.client_id.get_secret_value()
        client_secret = credentials.client_secret.get_secret_value()
        key = hashlib.sha256(json.dumps(
            [origin, client_id, client_secret], separators=(",", ":"),
        ).encode()).hexdigest()
        now = self.clock()
        cached = self._tokens.get(key)
        if cached is not None and cached.expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS:
            return cached
        body = urlencode({
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret,
        }).encode("ascii")
        content, _headers = await self._post(
            origin + SHOPIFY_TOKEN_PATH, body, "application/x-www-form-urlencoded", None,
            check_current, _token_rejected,
        )
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            raise _invalid() from None
        if not isinstance(payload, dict):
            raise _invalid()
        value, scope = payload.get("access_token"), payload.get("scope")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(value, str) or not 16 <= len(value) <= 4096
            or not _HEADER_SAFE.fullmatch(value)
            or not isinstance(scope, str) or len(scope) > 4000
            or type(expires_in) is not int or not 120 <= expires_in <= 172_800
        ):
            raise _invalid()
        scopes = frozenset(item for item in re.split(r"[\s,]+", scope) if item)
        if len(scopes) > 200 or not all(_SCOPE.fullmatch(item) for item in scopes):
            raise _invalid()
        token = _AccessToken(key=key, value=value, scopes=scopes, expires_at=now + expires_in)
        # _post rechecked the connector after the exchange, so a revoked or rotated
        # configuration never caches this token.
        self._tokens = {
            item_key: item for item_key, item in self._tokens.items() if item.expires_at > now
        }
        self._tokens[key] = token
        return token

    async def _post(
        self, url: str, body: bytes, content_type: str, token: _AccessToken | None,
        check_current: Callable[[], None], reject: Callable[[int], MarketplaceError],
    ) -> tuple[bytes, httpx.Headers]:
        destination = await pin_marketplace_destination(url)
        check_current()
        headers = {
            "Host": destination.host_header, "Content-Type": content_type,
            "Accept": "application/json", "Accept-Encoding": "identity",
        }
        if token is not None:
            headers["X-Shopify-Access-Token"] = token.value
        wire_request = httpx.Request(
            "POST", destination.request_url, content=body, headers=headers,
            extensions={
                "sni_hostname": destination.sni_hostname,
                "timeout": {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 5.0},
            },
        )
        content, response_headers = await send_marketplace_request(
            self.transport_factory, wire_request, reject,
        )
        check_current()
        return content, response_headers

    async def _graphql(
        self, origin: str, token: _AccessToken, query: str, variables: dict[str, Any],
        check_current: Callable[[], None], private_values: set[str],
    ) -> tuple[dict[str, Any], str]:
        def rejected(status: int) -> MarketplaceError:
            if status != 401:
                return _read_rejected(status)
            if self._tokens.get(token.key) is token:
                del self._tokens[token.key]
            return MarketplaceError(
                "MARKETPLACE_TOKEN_REJECTED",
                "Shopify rejected the access token. Read again to request a new one; if it "
                "repeats, confirm the app is still installed on the store.", 502,
            )

        body = json.dumps(
            {"query": query, "variables": variables}, separators=(",", ":"),
        ).encode("utf-8")
        content, headers = await self._post(
            origin + SHOPIFY_GRAPHQL_PATH, body, "application/json", token, check_current,
            rejected,
        )
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            raise _invalid() from None
        if not isinstance(payload, dict):
            raise _invalid()
        errors = payload.get("errors")
        if errors is not None:
            # GraphQL reports throttling and access failures with HTTP 200. Partial
            # data is never returned, and upstream messages are withheld.
            codes: set[str] = set()
            for item in errors[:50] if isinstance(errors, list) else []:
                extensions = item.get("extensions") if isinstance(item, dict) else None
                if isinstance(extensions, dict) and isinstance(extensions.get("code"), str):
                    codes.add(extensions["code"])
            if "THROTTLED" in codes:
                raise _rate_limited()
            if "ACCESS_DENIED" in codes:
                raise MarketplaceError(
                    "MARKETPLACE_SCOPE_REQUIRED",
                    "Shopify denied access to this data. Check the app's granted scopes.", 403,
                )
            if "SHOP_INACTIVE" in codes:
                raise MarketplaceError(
                    "MARKETPLACE_SHOP_UNAVAILABLE", "Shopify reports this store is inactive.", 502,
                )
            raise MarketplaceError(
                "MARKETPLACE_API_REJECTED",
                "Shopify rejected the read. Check the app's scopes and the store status.", 502,
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _invalid()
        request_id = headers.get("x-request-id", "")
        if not _REQUEST_ID.fullmatch(request_id) or any(
            private in request_id for private in private_values
        ):
            request_id = ""
        return data, request_id

    @staticmethod
    def _text(value: Any, private_values: set[str], limit: int) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise _invalid()
        for private in private_values:
            value = value.replace(private, "[redacted]")
        return sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD).text

    @staticmethod
    def _legacy_id(value: Any, resource: str) -> str:
        match = (
            re.fullmatch(rf"gid://shopify/{resource}/(\d{{1,20}})", value)
            if isinstance(value, str) else None
        )
        if match is None:
            raise _invalid()
        return match.group(1)

    @staticmethod
    def _timestamp(value: Any, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise _invalid()
        return value

    @staticmethod
    def _require_unique(items: list[dict[str, Any]]) -> None:
        if len({item["id"] for item in items}) != len(items):
            raise _invalid()

    def _shop(self, shop: Any, origin: str, private_values: set[str]) -> dict[str, str]:
        if not isinstance(shop, dict):
            raise _invalid()
        domain = shop.get("myshopifyDomain")
        if not isinstance(domain, str) or domain.casefold() != urlsplit(origin).hostname:
            raise MarketplaceError(
                "MARKETPLACE_SHOP_MISMATCH",
                "The credentials belong to a different Shopify store than the configured address.",
                409,
            )
        currency = shop.get("currencyCode")
        address = shop.get("shopAddress")
        country = address.get("countryCodeV2") if isinstance(address, dict) else None
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise _invalid()
        if country is not None and (
            not isinstance(country, str) or not _COUNTRY.fullmatch(country)
        ):
            raise _invalid()
        return {
            "id": self._legacy_id(shop.get("id"), "Shop"),
            "name": self._text(shop.get("name"), private_values, 1000),
            "region": country or "", "seller_type": "", "domain": domain.casefold(),
            "currency": currency,
        }

    def _require_shop(self, data: dict[str, Any], shop_id: str | None) -> None:
        shop = data.get("shop")
        if not isinstance(shop, dict):
            raise _invalid()
        if self._legacy_id(shop.get("id"), "Shop") != shop_id:
            raise MarketplaceError(
                "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                "The Shopify credentials do not authorize this shop.", 403,
            )

    @staticmethod
    def _page(
        connection: Any, page_size: int, private_values: set[str],
    ) -> tuple[list[Any], str | None]:
        if not isinstance(connection, dict):
            raise _invalid()
        nodes, info = connection.get("nodes"), connection.get("pageInfo")
        if not isinstance(nodes, list) or len(nodes) > page_size or not isinstance(info, dict):
            raise _invalid()
        has_next, cursor = info.get("hasNextPage"), info.get("endCursor")
        if type(has_next) is not bool:
            raise _invalid()
        if not has_next:
            return nodes, None
        # An opaque cursor must round-trip unchanged; reject reflected secrets.
        if (
            not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
            or any(ord(char) < 32 for char in cursor)
            or any(private in cursor for private in private_values)
        ):
            raise _invalid()
        return nodes, cursor

    def _product(self, node: Any, private_values: set[str]) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise _invalid()
        status = node.get("status")
        if not isinstance(status, str) or status == "ALL" or status not in SHOPIFY_PRODUCT_STATUSES:
            raise _invalid()
        return {
            "id": self._legacy_id(node.get("id"), "Product"),
            "title": self._text(node.get("title"), private_values, 2000),
            "status": status,
            "vendor": self._text(node.get("vendor"), private_values, 1000),
            "product_type": self._text(node.get("productType"), private_values, 1000),
            "updated_at": self._timestamp(node.get("updatedAt")),
        }

    def _order(self, node: Any, private_values: set[str]) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise _invalid()
        financial = node.get("displayFinancialStatus")
        fulfillment = node.get("displayFulfillmentStatus")
        quantity = node.get("currentSubtotalLineItemsQuantity")
        money = node.get("currentTotalPriceSet")
        shop_money = money.get("shopMoney") if isinstance(money, dict) else None
        amount = shop_money.get("amount") if isinstance(shop_money, dict) else None
        currency = shop_money.get("currencyCode") if isinstance(shop_money, dict) else None
        if (
            (financial is not None and (
                not isinstance(financial, str) or financial not in ORDER_FINANCIAL_STATUSES
            ))
            or not isinstance(fulfillment, str) or fulfillment not in ORDER_FULFILLMENT_STATUSES
            or type(quantity) is not int or not 0 <= quantity <= 1_000_000_000
            or type(node.get("test")) is not bool
            or not isinstance(amount, str) or not _MONEY.fullmatch(amount)
            or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
        ):
            raise _invalid()
        return {
            "id": self._legacy_id(node.get("id"), "Order"),
            "name": self._text(node.get("name"), private_values, 160),
            "created_at": self._timestamp(node.get("createdAt")),
            "cancelled_at": self._timestamp(node.get("cancelledAt"), nullable=True),
            "test": node["test"],
            "financial_status": financial, "fulfillment_status": fulfillment,
            "item_quantity": quantity, "total_amount": amount, "currency": currency,
        }
