"""RFC 8291 encryption / RFC 8292 VAPID, with a pinned public-only transport."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import Field, field_validator
from pywebpush import WebPusher

from .config import Settings, companion_origin
from .models import APIModel, utc_now
from .service_credentials import ServiceCredentialId, ServiceCredentialRegistry, parse_vapid_key
from .web_tools import send_pinned_public_request

WAKE_PAYLOAD = b'{"type":"ophira-companion-update"}'
_PRIVATE_PUSH_REQUEST = ContextVar("private_push_request", default=False)


class _PushRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpx logs full URLs; httpcore DEBUG traces can log capability-bearing
        # response headers. Suppress only this task's push request diagnostics.
        return not _PRIVATE_PUSH_REQUEST.get()


def _install_private_log_filters() -> None:
    # Logger filters do NOT run on ancestor loggers during propagation. Include
    # the transport's actual child loggers, even when a protocol imports lazily.
    names = {
        "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
        "httpcore.proxy", "httpcore.socks",
    }
    names.update(name for name in list(logging.Logger.manager.loggerDict)
                 if name.startswith(("httpx.", "httpcore.")))
    for name in names:
        target = logging.getLogger(name)
        if not any(isinstance(item, _PushRequestLogFilter) for item in target.filters):
            target.addFilter(_PushRequestLogFilter())


def decode_key(value: str, size: int) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("invalid push key")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != size:
        raise ValueError("invalid push key length")
    return decoded


def _retry_after_seconds(value: str, now: datetime) -> int:
    # RFC 9110 permits either delta-seconds or an HTTP-date. Ignoring the latter
    # retries earlier than the service requested. Bound both input and delay.
    value = value.strip()
    if not value or len(value) > 128:
        return 0
    if value.isascii() and value.isdecimal():
        return min(int(value), 3_600)
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:  # The obsolete asctime HTTP-date means GMT.
            deadline = deadline.replace(tzinfo=UTC)
        return max(0, min(math.ceil((deadline - now).total_seconds()), 3_600))
    except (ValueError, TypeError, OverflowError):
        return 0


class PushKeys(APIModel):
    p256dh: str = Field(min_length=86, max_length=88, repr=False)
    auth: str = Field(min_length=22, max_length=24, repr=False)

    @field_validator("p256dh")
    @classmethod
    def valid_point(cls, value: str) -> str:
        raw = decode_key(value, 65)
        if raw[0] != 4:
            raise ValueError("uncompressed P-256 point required")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        return value

    @field_validator("auth")
    @classmethod
    def valid_auth(cls, value: str) -> str:
        decode_key(value, 16)
        return value


class PushSubscription(APIModel):
    endpoint: str = Field(min_length=1, max_length=4_096, repr=False)
    expirationTime: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    keys: PushKeys = Field(repr=False)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or parsed.scheme != "https" or not parsed.hostname or parsed.fragment
            or parsed.username is not None or parsed.password is not None
            or "\\" in value or "%" in parsed.netloc or parsed.hostname.endswith(".")
            or parsed.port not in {None, 443} or not parsed.path.startswith("/")
        ):
            raise ValueError("a standard HTTPS push endpoint is required")
        return value

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.endpoint.encode()).hexdigest()

    @property
    def destination_identity(self) -> str:
        # Use the transport's URL normalization for uniqueness, while retaining
        # the browser's exact serialization for its reconciliation fingerprint.
        return str(httpx.URL(self.endpoint))

    def expired(self, now: datetime) -> bool:
        return self.expirationTime is not None and self.expirationTime <= now.timestamp() * 1_000


@dataclass(frozen=True)
class PushOutcome:
    status: str  # accepted is a push-service acknowledgement, not device delivery.
    reason: str
    retry_after: int = 0


class WebPushAdapter:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 credentials: ServiceCredentialRegistry | None = None):
        _install_private_log_filters()
        self.settings = settings
        self.transport = transport
        self.credentials = credentials or ServiceCredentialRegistry(settings)
        self._vapid = None
        self._public_key: str | None = None
        self._reason = "not_configured"
        self._credential_revision = None
        self._configuration = None
        self._refresh()

    def _clear(self, reason):
        self._vapid = None
        self._public_key = None
        self._reason = reason
        self._configuration = None

    def _refresh(self):
        settings = self.settings
        if not (
            settings.push_enabled and settings.companion_enabled
            and settings.push_vapid_subject and settings.push_allowed_origins
        ):
            self._clear("not_configured")
            return
        try:
            view, key = self.credentials.snapshot(ServiceCredentialId.WEB_PUSH)
        except (OSError, ValueError):
            self._clear("credential_unavailable")
            self._credential_revision = None
            return
        self._credential_revision = view["revision"]
        if not key:
            self._clear("not_configured")
            return
        configuration = (view["revision"], settings.push_vapid_subject)
        if configuration == self._configuration:
            return
        self._clear("invalid_configuration")
        try:
            vapid = parse_vapid_key(key)
            # Validate subject without a request or logging the private key.
            vapid.sign({"aud": "https://push.example", "sub": settings.push_vapid_subject})
            self._public_key = base64.urlsafe_b64encode(vapid.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
            )).decode().rstrip("=")
            self._vapid = vapid
            self._reason = ""
            self._configuration = configuration
        except Exception:
            # Third-party config exceptions can include secrets; no exception text escapes.
            self._clear("invalid_configuration")

    @property
    def public_key(self):
        self._refresh()
        return self._public_key

    @property
    def reason(self):
        self._refresh()
        return self._reason

    @property
    def enabled(self) -> bool:
        self._refresh()
        return self._public_key is not None and self._vapid is not None

    def validate(self, subscription: PushSubscription) -> None:
        parsed = urlsplit(subscription.endpoint)
        origin = companion_origin(f"https://{parsed.netloc}")
        if not self.enabled or origin not in self.settings.push_allowed_origins:
            raise ValueError("Push service origin is not enabled by the operator")
        if subscription.expired(utc_now()):
            raise ValueError("Push subscription has expired")

    async def send(self, subscription: PushSubscription, topic: str) -> PushOutcome:
        try:
            self.validate(subscription)
        except ValueError:
            return PushOutcome("disabled", "subscription_unavailable")
        vapid, revision = self._vapid, self._credential_revision
        parsed = urlsplit(subscription.endpoint)
        headers = vapid.sign({
            "aud": companion_origin(f"https://{parsed.netloc}"),
            "sub": self.settings.push_vapid_subject,
            "exp": int(utc_now().timestamp()) + 12 * 60 * 60,
        })
        encoded = WebPusher(subscription.model_dump()).encode(WAKE_PAYLOAD)
        headers.update({
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
            "TTL": "300", "Urgency": "normal", "Topic": topic,
        })
        log_token = _PRIVATE_PUSH_REQUEST.set(True)
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=10, trust_env=False, follow_redirects=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client:
                self._refresh()
                if self._vapid is None or self._credential_revision != revision:
                    return PushOutcome("disabled", "application_key_changed")
                async with asyncio.timeout(15):
                    response, _reason = await send_pinned_public_request(
                        client, "POST", subscription.endpoint, headers=headers,
                        content=encoded["body"], stream=True,
                    )
                if response is None:
                    return PushOutcome("retry", "destination_unavailable")
                try:
                    status = response.status_code
                    retry_after = _retry_after_seconds(
                        response.headers.get("Retry-After", ""), utc_now(),
                    )
                finally:
                    await response.aclose()  # Response bodies may contain endpoint secrets.
        except (httpx.HTTPError, OSError, TimeoutError):
            return PushOutcome("retry", "transport_unavailable")
        finally:
            _PRIVATE_PUSH_REQUEST.reset(log_token)
        if 200 <= status < 300:
            return PushOutcome("accepted", "service_accepted")
        if status in {401, 403, 404, 410}:
            return PushOutcome("disabled", "subscription_rejected")
        if status == 429 or status >= 500:
            return PushOutcome("retry", "service_busy", retry_after)
        return PushOutcome("failed", "service_rejected")
