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
from .models import Connector, utc_now
from .providers import FileSecretVault

CONNECTOR_CATALOG: list[dict[str, str | None]] = [
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
        "id": "shopify",
        "type": "shopify",
        "display_name": "Shopify",
        "scope": "orders:read products:write",
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
    def __init__(self, root: Path, vault: FileSecretVault | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = vault
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
            vaulted = self.vault is not None and self.vault.has(connector.id)
            env_present = bool(connector.secret_env and env.get(connector.secret_env))
            reason = check_commercial_https_endpoint(connector.endpoint or "")
            if reason:
                health = "unreachable"
            elif connector.secret_env and not vaulted and not env_present:
                health = "no_credential"
            elif not vaulted and not env_present:
                health = "no_credential"
            else:
                health = "ok"
        else:
            vaulted = self.vault is not None and self.vault.has(connector.id)
            env_present = bool(connector.secret_env and env.get(connector.secret_env))
            health = (
                "no_credential"
                if connector.secret_env and not vaulted and not env_present
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
        secret = self.vault.get(connector.id) if self.vault is not None else None
        if not secret and connector.secret_env:
            secret = env.get(connector.secret_env)
        if not secret:
            raise ConnectorUnavailableError(
                "COMMERCIAL_CONNECTOR_NO_CREDENTIAL",
                "The commercial action connector has no credential.",
            )
        return CommercialConnectorRuntime(
            connector=connector,
            credential=SecretStr(secret),
        )
