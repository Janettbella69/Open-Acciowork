"""FR-21 connector registry: catalog, vaulted secrets, revoke, honest health."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr

from .constants import COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE
from .marketplaces import (
    SHOPIFY_TYPE,
    TIKTOK_AUTH_SCOPE,
    TIKTOK_SHOP_ORIGIN,
    TIKTOK_SHOP_TYPE,
    parse_tiktok_shop_credentials,
)
from .models import Connector, utc_now
from .providers import FileSecretVault
from .secret_vault import CredentialChangedError
from .shein import (
    SHEIN_DEFAULT_SCOPE,
    SHEIN_ORIGIN,
    SHEIN_ORIGINS,
    SHEIN_TYPE,
    parse_shein_credentials,
)
from .shopee import (
    SHOPEE_DEFAULT_SCOPE,
    SHOPEE_ORIGIN,
    SHOPEE_ORIGINS,
    SHOPEE_TYPE,
    parse_shopee_credentials,
)
from .shopify import (
    SHOPIFY_DEFAULT_SCOPE,
    check_shopify_store_endpoint,
    parse_shopify_credentials,
)
from .temu import (
    TEMU_DEFAULT_SCOPE,
    TEMU_ORIGIN,
    TEMU_ORIGINS,
    TEMU_TYPE,
    parse_temu_credentials,
)

CONNECTOR_CATALOG: list[dict[str, str | None]] = [
    {
        "id": TEMU_TYPE, "type": TEMU_TYPE, "display_name": "Temu",
        "scope": TEMU_DEFAULT_SCOPE, "secret_env": "OPHIRA_CONNECTOR_TEMU_KEY",
        "endpoint": TEMU_ORIGIN,
    },
    {
        "id": SHOPEE_TYPE, "type": SHOPEE_TYPE, "display_name": "Shopee",
        "scope": SHOPEE_DEFAULT_SCOPE, "secret_env": "OPHIRA_CONNECTOR_SHOPEE_KEY",
        "endpoint": SHOPEE_ORIGIN,
    },
    {
        "id": SHEIN_TYPE,
        "type": SHEIN_TYPE,
        "display_name": "SHEIN",
        "scope": SHEIN_DEFAULT_SCOPE,
        "secret_env": "OPHIRA_CONNECTOR_SHEIN_KEY",
        "endpoint": SHEIN_ORIGIN,
    },
    {
        "id": "gmail",
        "type": "gmail",
        "display_name": "Gmail",
        "scope": "mail:read mail:send",
        "secret_env": "OPHIRA_CONNECTOR_GMAIL_KEY",
    },
    {
        "id": "outlook",
        "type": "outlook",
        "display_name": "Outlook",
        "scope": "mail:read mail:send",
        "secret_env": "OPHIRA_CONNECTOR_OUTLOOK_KEY",
    },
    {
        "id": "amazon_seller",
        "type": "amazon_seller",
        "display_name": "Amazon Seller Central",
        "scope": "orders:read listings:write",
        "secret_env": "OPHIRA_CONNECTOR_AMAZON_KEY",
    },
    {
        "id": SHOPIFY_TYPE,
        "type": SHOPIFY_TYPE,
        "display_name": "Shopify",
        "scope": SHOPIFY_DEFAULT_SCOPE,
        "secret_env": "OPHIRA_CONNECTOR_SHOPIFY_KEY",
    },
    {
        "id": "alibaba",
        "type": "alibaba",
        "display_name": "Alibaba / AliExpress",
        "scope": "rfq:read messages:write",
        "secret_env": "OPHIRA_CONNECTOR_ALIBABA_KEY",
    },
    {
        "id": "instagram",
        "type": "instagram",
        "display_name": "Instagram",
        "scope": "media:read comments:read",
        "secret_env": "OPHIRA_CONNECTOR_INSTAGRAM_KEY",
    },
    {
        "id": "tiktok",
        "type": "tiktok",
        "display_name": "TikTok",
        "scope": "video:read comments:read",
        "secret_env": "OPHIRA_CONNECTOR_TIKTOK_KEY",
    },
    {
        "id": TIKTOK_SHOP_TYPE,
        "type": TIKTOK_SHOP_TYPE,
        "display_name": "TikTok Shop",
        "scope": TIKTOK_AUTH_SCOPE,
        "secret_env": "OPHIRA_CONNECTOR_TIKTOK_SHOP_KEY",
        "endpoint": TIKTOK_SHOP_ORIGIN,
    },
    {
        "id": "wechat",
        "type": "wechat",
        "display_name": "WeChat",
        "scope": "message:send docs:read",
        "secret_env": "OPHIRA_CONNECTOR_WECHAT_KEY",
    },
    {
        "id": "feishu",
        "type": "feishu",
        "display_name": "Feishu / Lark",
        "scope": "im:send docs:read bitable:write",
        "secret_env": "OPHIRA_CONNECTOR_FEISHU_KEY",
    },
    {
        "id": "image_gen",
        "type": "image_gen",
        "display_name": "Image generation",
        "scope": "media:generate",
        "secret_env": "OPHIRA_MEDIA_API_KEY",
    },
    {
        "id": "chrome_cdp",
        "type": "chrome_cdp",
        "display_name": "Chrome DevTools (GUI L2)",
        "scope": "browser:attach",
        "secret_env": None,
        "endpoint": "http://127.0.0.1:9222",
    },
    {
        "id": "search",
        "type": "search",
        "display_name": "Public web search",
        "scope": "web:search",
        "secret_env": "OPHIRA_SEARCH_API_KEY",
    },
]

# Connector credentials are server-only data. Restrict administrator-selected
# environment references to the dedicated connector namespace so a connector
# cannot be pointed at process-control credentials such as PATH, HOME, or an
# ANTHROPIC_* provider token. The two legacy application-owned names below are
# retained because they are part of the built-in catalog.
_CONNECTOR_SECRET_ENV_PATTERN = re.compile(
    r"^(?:OPHIRA_CONNECTOR_[A-Z][A-Z0-9_]{2,95}|"
    r"OPHIRA_(?:MEDIA|SEARCH)_API_KEY)$"
)


class ConnectorNotFoundError(KeyError):
    pass


class ConnectorUnavailableError(RuntimeError):
    """A configured connector cannot currently be used by a runtime tool."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_connector_secret_env(secret_env: str | None) -> str | None:
    """Validate a server-owned connector credential environment reference."""

    if secret_env is None:
        return None
    if not _CONNECTOR_SECRET_ENV_PATTERN.fullmatch(secret_env):
        return (
            "connector secret_env must use OPHIRA_CONNECTOR_* "
            "(or the built-in OPHIRA_MEDIA_API_KEY/OPHIRA_SEARCH_API_KEY names)"
        )
    return None


def is_public_unicast_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Accept only globally routable unicast destinations for credentialed calls."""

    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


@dataclass(frozen=True)
class CommercialConnectorRuntime:
    """Server-only connector material passed directly to an action adapter."""

    connector: Connector
    credential: SecretStr = field(repr=False)
    credential_revision: str | None = field(default=None, repr=False)


def check_commercial_https_endpoint(endpoint: str) -> str | None:
    """Validate the admin-configured base URL used for external mutations.

    Domain names are resolved again immediately before every adapter request.
    Literal local/private/reserved destinations can be rejected here without a
    network lookup, so they can never be persisted as usable commerce endpoints.
    """

    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except (TypeError, ValueError):
        return "commercial action endpoint could not be parsed"
    if parts.scheme != "https":
        return "commercial action endpoint must use HTTPS"
    if not parts.hostname:
        return "commercial action endpoint requires a hostname"
    if parts.username or parts.password:
        return "commercial action endpoint cannot contain credentials"
    if parts.query or parts.fragment:
        return "commercial action endpoint cannot contain query or fragment"
    if port is not None and not (1 <= port <= 65_535):
        return "commercial action endpoint port is invalid"
    hostname = parts.hostname.rstrip(".").casefold()
    if hostname == "localhost":
        return "commercial action endpoint must use a public network destination"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not is_public_unicast_address(address):
            return "commercial action endpoint must use a public network destination"
    return None


def check_loopback_cdp_endpoint(endpoint: str) -> str | None:
    """Reject CDP endpoints that could turn browser attachment into SSRF.

    Playwright accepts HTTP(S) and WebSocket CDP endpoints. OPHIRA deliberately
    narrows that surface to an explicit loopback literal (or exact ``localhost``)
    with a port, so a model can never steer attachment to another host.
    """
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except (TypeError, ValueError):
        return "CDP endpoint 无法解析"
    if parts.scheme not in {"http", "https", "ws", "wss"}:
        return "CDP endpoint 仅允许 http/https/ws/wss"
    if not parts.hostname or port is None:
        return "CDP endpoint 必须包含 loopback 主机与显式端口"
    if parts.username or parts.password:
        return "CDP endpoint 不允许携带凭据"
    if parts.query or parts.fragment:
        return "CDP endpoint 不允许包含 query 或 fragment"

    hostname = parts.hostname.rstrip(".").casefold()
    if hostname == "localhost":
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "CDP endpoint 主机必须是 localhost 或 loopback IP"
    if not address.is_loopback:
        return "CDP endpoint 主机必须是 loopback 地址"
    return None


def _cdp_endpoint_reachable(endpoint: str, timeout_seconds: float = 0.75) -> bool:
    """Use a bounded TCP probe; Playwright performs the protocol handshake later."""
    parts = urlsplit(endpoint)
    assert parts.hostname is not None and parts.port is not None
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


class ConnectorRegistry:
    def __init__(self, root: Path, vault: FileSecretVault | None = None, *,
                 credential_defaults: Mapping[str, SecretStr | None] | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = vault
        self._credential_defaults = dict(credential_defaults or {})
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid connector id")
        return value

    def _path(self, connector_id: str) -> Path:
        return self.root / f"{self._safe_id(connector_id)}.json"

    def _write(self, connector: Connector) -> None:
        path = self._path(connector.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(connector.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def ensure_catalog(self) -> list[Connector]:
        for item in CONNECTOR_CATALOG:
            connector_id = str(item["id"])
            if self._path(connector_id).exists():
                continue
            self.upsert(
                connector_id,
                type=str(item["type"]),
                display_name=str(item["display_name"]),
                scope=str(item.get("scope") or ""),
                secret_env=item.get("secret_env"),  # type: ignore[arg-type]
                endpoint=item.get("endpoint"),  # type: ignore[arg-type]
            )
        return self.list()

    def list(self) -> list[Connector]:
        connectors: list[Connector] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                connectors.append(Connector.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return connectors

    def get(self, connector_id: str) -> Connector:
        path = self._path(connector_id)
        if not path.exists():
            raise ConnectorNotFoundError(connector_id)
        return Connector.model_validate_json(path.read_text("utf-8"))

    def upsert(
        self,
        connector_id: str,
        *,
        type: str,
        display_name: str,
        scope: str = "",
        secret_env: str | None = None,
        endpoint: str | None = None,
        secret: str | None = None,
    ) -> Connector:
        connector_id = self._safe_id(connector_id)
        secret_env_reason = check_connector_secret_env(secret_env)
        if secret_env_reason:
            raise ValueError(secret_env_reason)
        if connector_id == "chrome_cdp" or type == "chrome_cdp":
            if connector_id != "chrome_cdp" or type != "chrome_cdp":
                raise ValueError("Chrome CDP connector 必须使用 id/type chrome_cdp")
            reason = check_loopback_cdp_endpoint(endpoint or "")
            if reason:
                raise ValueError(reason)
        if type == COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE:
            reason = check_commercial_https_endpoint(endpoint or "")
            if reason:
                raise ValueError(reason)
        if type == TIKTOK_SHOP_TYPE:
            if endpoint != TIKTOK_SHOP_ORIGIN:
                raise ValueError("TikTok Shop requires its official API origin")
            if secret is not None:
                parse_tiktok_shop_credentials(secret)
                if self.vault is None:
                    raise ValueError("connector vault is not configured")
        if type == SHOPIFY_TYPE:
            # The catalog record starts without a store; credentials always need one.
            if endpoint is not None or secret is not None:
                reason = check_shopify_store_endpoint(endpoint)
                if reason:
                    raise ValueError(reason)
            if secret is not None:
                parse_shopify_credentials(secret)
                if self.vault is None:
                    raise ValueError("connector vault is not configured")
        if type == TEMU_TYPE:
            if endpoint not in TEMU_ORIGINS:
                raise ValueError("Temu requires an official regional API origin")
            if secret is not None:
                parse_temu_credentials(secret)
                if self.vault is None:
                    raise ValueError("connector vault is not configured")
        if type == SHOPEE_TYPE:
            if endpoint not in SHOPEE_ORIGINS:
                raise ValueError("Shopee requires an official API origin")
            if secret is not None:
                parse_shopee_credentials(secret)
                if self.vault is None:
                    raise ValueError("connector vault is not configured")
        if type == SHEIN_TYPE:
            if endpoint not in SHEIN_ORIGINS:
                raise ValueError("SHEIN requires an official API origin")
            if secret is not None:
                parse_shein_credentials(secret)
                if self.vault is None:
                    raise ValueError("connector vault is not configured")
        existing = None
        try:
            existing = self.get(connector_id)
        except ConnectorNotFoundError:
            pass
        connector = Connector(
            id=connector_id,
            type=type,
            display_name=display_name,
            scope=scope,
            secret_env=secret_env,
            endpoint=endpoint,
            # An explicit PUT is also the reconnect action after revoke. Any
            # configuration change must be health-checked again before runtime use.
            enabled=True,
            health="unknown",
            last_checked_at=None,
            created_at=existing.created_at if existing else utc_now(),
        )
        with self._lock:
            self._write(connector)
            if secret and self.vault is not None:
                self.vault.set(connector.id, secret)
        return connector

    def store_secret(self, connector_id: str, secret: str) -> Connector:
        connector = self.get(connector_id)
        if connector.type == TIKTOK_SHOP_TYPE:
            parse_tiktok_shop_credentials(secret)
        elif connector.type == SHOPIFY_TYPE:
            parse_shopify_credentials(secret)
        elif connector.type == SHEIN_TYPE:
            parse_shein_credentials(secret)
        elif connector.type == TEMU_TYPE:
            parse_temu_credentials(secret)
        elif connector.type == SHOPEE_TYPE:
            parse_shopee_credentials(secret)
        if self.vault is None:
            raise ValueError("connector vault is not configured")
        self.vault.set(connector.id, secret)
        return connector

    def revoke(self, connector_id: str) -> Connector:
        connector = self.get(connector_id)
        if self.vault is not None:
            self.vault.delete(connector.id)
        updated = connector.model_copy(
            update={"enabled": False, "health": "unknown", "last_checked_at": utc_now()}
        )
        with self._lock:
            self._write(updated)
        return updated

    def health_check(
        self,
        connector_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> Connector:
        env = os.environ if environ is None else environ
        connector = self.get(connector_id)
        if not connector.enabled:
            health = "unknown"
        elif connector.id == "chrome_cdp" and connector.type == "chrome_cdp":
            reason = check_loopback_cdp_endpoint(connector.endpoint or "")
            health = (
                "unreachable"
                if reason or not _cdp_endpoint_reachable(connector.endpoint or "")
                else "ok"
            )
        elif connector.type == COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE:
            configured = bool(self.credential(connector, environ=env))
            reason = check_commercial_https_endpoint(connector.endpoint or "")
            if reason:
                health = "unreachable"
            elif not configured:
                health = "no_credential"
            else:
                health = "ok"
        else:
            configured = bool(self.credential(connector, environ=env))
            health = (
                "no_credential"
                if connector.secret_env and not configured
                else "ok"
            )
        updated = connector.model_copy(
            update={"health": health, "last_checked_at": utc_now()}
        )
        with self._lock:
            self._write(updated)
        return updated

    def require_healthy_browser_endpoint(self) -> str:
        """Resolve the sole L2 endpoint from durable connector state.

        Tool input never carries an endpoint. This method is the only bridge
        between the browser MCP and a user-configured Chrome attachment.
        """
        try:
            connector = self.get("chrome_cdp")
        except ConnectorNotFoundError as exc:
            raise ConnectorUnavailableError(
                "BROWSER_CONNECTOR_NOT_CONFIGURED",
                "Chrome CDP connector 尚未配置。",
            ) from exc
        if connector.type != "chrome_cdp":
            raise ConnectorUnavailableError(
                "BROWSER_CONNECTOR_INVALID",
                "Chrome CDP connector 类型无效。",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "BROWSER_CONNECTOR_DISABLED",
                "Chrome CDP connector 已停用。",
            )
        if connector.health != "ok":
            raise ConnectorUnavailableError(
                "BROWSER_CONNECTOR_UNHEALTHY",
                "Chrome CDP connector 未通过健康检查。",
            )
        reason = check_loopback_cdp_endpoint(connector.endpoint or "")
        if reason:
            raise ConnectorUnavailableError("BROWSER_CONNECTOR_INVALID", reason)
        assert connector.endpoint is not None
        return connector.endpoint

    def environment_credential(self, connector: Connector, *, environ=None) -> str | None:
        if not connector.secret_env:
            return None
        if check_connector_secret_env(connector.secret_env):
            raise ValueError("Invalid connector credential reference")
        value = (os.environ if environ is None else environ).get(connector.secret_env)
        default = self._credential_defaults.get(connector.secret_env)
        return value if value is not None else default.get_secret_value() if default else None

    def credential(self, connector: Connector, *, environ=None) -> str | None:
        fallback = self.environment_credential(connector, environ=environ)
        if self.vault is None:
            return fallback
        return self.vault.resolve(connector.id, fallback=fallback)

    def require_tiktok_shop_runtime(self, connector_id: str) -> CommercialConnectorRuntime:
        """Resolve server-only seller material; health is never remote verification."""
        try:
            connector = self.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        if connector.type != TIKTOK_SHOP_TYPE or connector.endpoint != TIKTOK_SHOP_ORIGIN:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "MARKETPLACE_DISABLED", "The marketplace connector is revoked.",
            )
        secret = self.credential(connector)
        if not secret:
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_REQUIRED", "Configure a seller access token first.",
            )
        try:
            parse_tiktok_shop_credentials(secret)
        except ValueError:
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_INVALID", "Replace the invalid seller credential bundle.",
            ) from None
        return CommercialConnectorRuntime(connector=connector, credential=SecretStr(secret))

    def require_shopify_runtime(self, connector_id: str) -> CommercialConnectorRuntime:
        """Resolve a store address and app credentials; health is never remote verification."""
        try:
            connector = self.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        if connector.type != SHOPIFY_TYPE:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "MARKETPLACE_DISABLED", "The marketplace connector is revoked.",
            )
        if check_shopify_store_endpoint(connector.endpoint):
            raise ConnectorUnavailableError(
                "MARKETPLACE_STORE_REQUIRED", "Enter the store's myshopify.com address first.",
            )
        secret = self.credential(connector)
        if not secret:
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_REQUIRED",
                "Configure the Shopify app client ID and client secret first.",
            )
        try:
            parse_shopify_credentials(secret)
        except ValueError:
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_INVALID", "Replace the invalid Shopify app credentials.",
            ) from None
        return CommercialConnectorRuntime(connector=connector, credential=SecretStr(secret))

    def require_shein_runtime(self, connector_id: str) -> CommercialConnectorRuntime:
        try:
            connector = self.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        if connector.type != SHEIN_TYPE or connector.endpoint not in SHEIN_ORIGINS:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "MARKETPLACE_DISABLED", "The marketplace connector is revoked.",
            )
        try:
            fallback = self.environment_credential(connector)
            if self.vault is None:
                raise ValueError
            view, secret = self.vault.resolve_versioned(connector.id, fallback=fallback)
            if not secret:
                raise ValueError
            parse_shein_credentials(secret)
        except (OSError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_INVALID", "Configure valid SHEIN seller credentials first.",
            ) from None
        return CommercialConnectorRuntime(connector=connector, credential=SecretStr(secret),
                                          credential_revision=view["revision"])

    def require_temu_runtime(self, connector_id: str) -> CommercialConnectorRuntime:
        try:
            connector = self.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        if connector.type != TEMU_TYPE or connector.endpoint not in TEMU_ORIGINS:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "MARKETPLACE_DISABLED", "The marketplace connector is revoked.",
            )
        try:
            fallback = self.environment_credential(connector)
            if self.vault is None:
                raise ValueError
            view, secret = self.vault.resolve_versioned(connector.id, fallback=fallback)
            if not secret:
                raise ValueError
            parse_temu_credentials(secret)
        except (OSError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_INVALID", "Configure valid Temu seller credentials.",
            ) from None
        return CommercialConnectorRuntime(connector=connector, credential=SecretStr(secret),
                                          credential_revision=view["revision"])

    def require_shopee_runtime(self, connector_id: str) -> CommercialConnectorRuntime:
        try:
            connector = self.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        if connector.type != SHOPEE_TYPE or connector.endpoint not in SHOPEE_ORIGINS:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "MARKETPLACE_DISABLED", "The marketplace connector is revoked.",
            )
        try:
            fallback = self.environment_credential(connector)
            if self.vault is None:
                raise ValueError
            view, secret = self.vault.resolve_versioned(connector.id, fallback=fallback)
            if not secret:
                raise ValueError
            parse_shopee_credentials(secret)
        except (OSError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_CREDENTIAL_INVALID", "Configure valid Shopee seller credentials.",
            ) from None
        return CommercialConnectorRuntime(connector=connector, credential=SecretStr(secret),
                                          credential_revision=view["revision"])

    def replace_shopee_credentials(self, runtime, raw):
        """Rotate only the captured vault generation; revoke cannot resurrect credentials."""
        parse_shopee_credentials(raw)
        connector = runtime.connector

        def configuration_current():
            # Do not resolve the vault here: this callback runs under its process lock.
            if self.get(connector.id) != connector:
                raise CredentialChangedError("Connector configuration changed")

        if self.require_shopee_runtime(connector.id) != runtime:
            raise CredentialChangedError("Connector credential changed")
        assert self.vault is not None and runtime.credential_revision is not None
        self.vault.set(
            connector.id, raw, expected_revision=runtime.credential_revision,
            fallback=self.environment_credential(connector), authorize=configuration_current,
        )
        updated = self.require_shopee_runtime(connector.id)
        if updated.connector != connector or updated.credential.get_secret_value() != raw:
            raise CredentialChangedError("Connector changed during rotation")
        return updated

    def require_commercial_action_runtime(
        self,
        connector_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> CommercialConnectorRuntime:
        """Resolve an enabled action connector without serializing its secret."""

        env = os.environ if environ is None else environ
        try:
            connector = self.get(connector_id)
        except ConnectorNotFoundError as exc:
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_NOT_CONFIGURED",
                "The commercial action connector is not configured.",
            ) from exc
        if connector.type != COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE:
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_UNSUPPORTED",
                "The connector does not provide the HTTPS JSON action contract.",
            )
        if not connector.enabled:
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_DISABLED",
                "The commercial action connector is disabled.",
            )
        if connector.health != "ok":
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_UNHEALTHY",
                "The commercial action connector has not passed its health check.",
            )
        reason = check_commercial_https_endpoint(connector.endpoint or "")
        if reason:
            raise ConnectorUnavailableError("COMMERCIAL_CONNECTOR_INVALID", reason)
        secret = self.credential(connector, environ=env)
        if not secret:
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_NO_CREDENTIAL",
                "The commercial action connector has no credential.",
            )
        return CommercialConnectorRuntime(
            connector=connector,
            credential=SecretStr(secret),
        )
