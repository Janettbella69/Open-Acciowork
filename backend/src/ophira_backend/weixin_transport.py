"""Bounded HTTP transport for Tencent's official Weixin direct-message protocol.

Protocol source: Tencent/openclaw-weixin, docs/protocol_zh_CN.md and
src/{api/api.ts,api/types.ts,auth/login-qr.ts}, reviewed 2026-09-29.
This module owns no account, cursor, login session, retry loop or Agent runtime.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx

from .web_tools import send_pinned_public_request

WEIXIN_API_ORIGIN = "https://ilinkai.weixin.qq.com"
_API_HOSTS = frozenset({"ilinkai.weixin.qq.com"})
# Pin the reviewed wire contract, with our own bot attribution below.
_PROTOCOL_VERSION = "2.4.9"
_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 9)
MAX_WEIXIN_RESPONSE_BYTES = 1024 * 1024
MAX_WEIXIN_MESSAGES = 100
MAX_WEIXIN_TEXT_CHARS = 4000
_PRIVATE_REQUEST = ContextVar("private_weixin_request", default=False)

WeixinLoginState = Literal[
    "wait", "scaned", "confirmed", "expired", "need_verifycode",
    "verify_code_blocked", "scaned_but_redirect", "binded_redirect",
]
_LOGIN_STATES = frozenset({
    "wait", "scaned", "confirmed", "expired", "need_verifycode",
    "verify_code_blocked", "scaned_but_redirect", "binded_redirect",
})


class WeixinTransportError(RuntimeError):
    """Only static diagnostics; never include upstream bodies, URLs or exceptions."""

    def __init__(
        self, code: str, *, retryable: bool = False, outcome_unknown: bool = False,
    ) -> None:
        super().__init__(f"Weixin request failed ({code}).")
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class WeixinQrCode:
    qrcode: str = field(repr=False)
    # This is QR payload text, not a trusted image URL. Never fetch it here.
    qrcode_img_content: str = field(repr=False)


@dataclass(frozen=True)
class WeixinLoginStatus:
    status: WeixinLoginState
    bot_token: str | None = field(default=None, repr=False)
    ilink_bot_id: str | None = field(default=None, repr=False)
    ilink_user_id: str | None = field(default=None, repr=False)
    base_url: str | None = None
    redirect_host: str | None = None


@dataclass(frozen=True)
class WeixinUpdates:
    # Raw bounded messages remain untrusted; the channel validates identity/type.
    msgs: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    get_updates_buf: str | None = field(default=None, repr=False)
    longpolling_timeout_ms: int | None = None


@dataclass(frozen=True)
class WeixinSendResult:
    # ret:0 may omit a server message ID. client_id is correlation, not delivery proof.
    message_id: str | None
    client_id: str


class _PrivateRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_REQUEST.get()


def _install_private_log_filters() -> None:
    # Like push_transport: ancestor filters do not cover propagated child records.
    # QR query values and even HTTP response headers can contain capabilities.
    names = {
        "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
        "httpcore.proxy", "httpcore.socks",
    }
    names.update(name for name in list(logging.Logger.manager.loggerDict)
                 if name.startswith(("httpx.", "httpcore.")))
    for name in names:
        target = logging.getLogger(name)
        if not any(isinstance(item, _PrivateRequestLogFilter) for item in target.filters):
            target.addFilter(_PrivateRequestLogFilter())


def validate_weixin_base_url(value: str) -> str:
    """Canonicalize only the reviewed HTTPS origin; no arbitrary IDC hosts."""
    try:
        if not isinstance(value, str) or len(value) > 256 or any(c.isspace() for c in value):
            raise ValueError
        parts = urlsplit(value)
        if (
            parts.scheme != "https" or parts.hostname not in _API_HOSTS
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or parts.path not in ("", "/")
            or parts.query or parts.fragment or "\\" in value
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise WeixinTransportError("DESTINATION_REJECTED") from None
    return WEIXIN_API_ORIGIN


def _string(
    value: Any, *, maximum: int, allow_empty: bool = False,
    error: str = "INVALID_ARGUMENT", multiline: bool = False,
) -> str:
    if (
        not isinstance(value, str) or len(value) > maximum
        or (not allow_empty and not value)
        or any((ord(c) < 32 and not (multiline and c in "\n\r\t"))
               or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise WeixinTransportError(error)
    return value


def _optional_string(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _string(value, maximum=maximum, error="MALFORMED_RESPONSE")


def _message_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        if int(value) < 2**64:
            return value
    raise WeixinTransportError("MALFORMED_RESPONSE", outcome_unknown=True)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _bounded_json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw, object_pairs_hook=_json_object, parse_constant=_reject_json_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise WeixinTransportError("MALFORMED_RESPONSE") from None
    if not isinstance(payload, dict):
        raise WeixinTransportError("MALFORMED_RESPONSE")
    pending = [(payload, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 20:
            raise WeixinTransportError("RESPONSE_TOO_LARGE")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise WeixinTransportError("MALFORMED_RESPONSE")
        elif isinstance(value, str):
            _string(value, maximum=MAX_WEIXIN_RESPONSE_BYTES, allow_empty=True,
                    multiline=True, error="MALFORMED_RESPONSE")
    return payload


def _check_api_result(payload: dict[str, Any], *, require_success: bool) -> None:
    for key in ("ret", "errcode"):
        if key not in payload:
            continue
        code = payload[key]
        if not isinstance(code, int) or isinstance(code, bool):
            raise WeixinTransportError("MALFORMED_RESPONSE")
        if code != 0:
            raise WeixinTransportError("SESSION_EXPIRED" if code == -14 else "API_REJECTED")
    if require_success and payload.get("ret") != 0:
        raise WeixinTransportError("MALFORMED_RESPONSE")


class WeixinTransport:
    """One bounded request per call, with no automatic resend on any outcome."""

    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15, poll_timeout_seconds: float = 35,
    ) -> None:
        for timeout in (timeout_seconds, poll_timeout_seconds):
            if isinstance(timeout, bool) or not isinstance(timeout, (float, int)):
                raise WeixinTransportError("INVALID_ARGUMENT")
            if not math.isfinite(timeout) or not 0 < timeout <= 60:
                raise WeixinTransportError("INVALID_ARGUMENT")
        self._transport = transport
        self._timeout = timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        _install_private_log_filters()

    async def _request(
        self, method: str, path: str, *, base_url: str = WEIXIN_API_ORIGIN,
        body: dict[str, Any] | None = None, token: str | None = None,
        poll: bool = False, is_send: bool = False,
    ) -> dict[str, Any]:
        origin = validate_weixin_base_url(base_url)
        headers = {
            "iLink-App-Id": "bot", "iLink-App-ClientVersion": _CLIENT_VERSION,
            "Accept": "application/json", "Accept-Encoding": "identity",
        }
        if method == "POST":
            headers.update({
                "Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            })
        if token is not None:
            _string(token, maximum=8192)
            if not token.isascii() or any(c.isspace() for c in token):
                raise WeixinTransportError("INVALID_ARGUMENT")
            headers["Authorization"] = f"Bearer {token}"
        timeout = self._poll_timeout if poll else self._timeout
        marker = _PRIVATE_REQUEST.set(True)
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=timeout, follow_redirects=False,
                    trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
                ) as client:
                    response, reason = await send_pinned_public_request(
                        client, method, origin + path, headers=headers, json_body=body, stream=True,
                    )
                    if response is None or reason:
                        raise WeixinTransportError("DESTINATION_REJECTED")
                    try:
                        if 300 <= response.status_code < 400:
                            raise WeixinTransportError(
                                "REDIRECT_REJECTED", outcome_unknown=is_send,
                            )
                        if not 200 <= response.status_code < 300:
                            raise WeixinTransportError(
                                f"HTTP_{response.status_code}",
                                retryable=not is_send and (
                                    response.status_code == 429 or response.status_code >= 500
                                ),
                                outcome_unknown=is_send and (
                                    response.status_code == 408 or response.status_code >= 500
                                ),
                            )
                        encoding = response.headers.get("content-encoding", "identity").lower()
                        if encoding not in ("", "identity"):
                            raise WeixinTransportError("MALFORMED_RESPONSE")
                        declared = response.headers.get("content-length")
                        if declared is not None:
                            try:
                                size = int(declared)
                            except ValueError:
                                raise WeixinTransportError("MALFORMED_RESPONSE") from None
                            if size < 0 or size > MAX_WEIXIN_RESPONSE_BYTES:
                                raise WeixinTransportError("RESPONSE_TOO_LARGE")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > MAX_WEIXIN_RESPONSE_BYTES:
                                raise WeixinTransportError("RESPONSE_TOO_LARGE")
                            raw.extend(chunk)
                        return _bounded_json(bytes(raw))
                    finally:
                        await response.aclose()
        except WeixinTransportError as exc:
            if is_send and exc.code in {"MALFORMED_RESPONSE", "RESPONSE_TOO_LARGE"}:
                raise WeixinTransportError(exc.code, outcome_unknown=True) from None
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise WeixinTransportError(
                "TIMEOUT", retryable=not is_send, outcome_unknown=is_send,
            ) from None
        except (httpx.HTTPError, OSError):
            raise WeixinTransportError(
                "NETWORK_ERROR", retryable=not is_send, outcome_unknown=is_send,
            ) from None
        finally:
            _PRIVATE_REQUEST.reset(marker)

    @staticmethod
    def _base_info() -> dict[str, str]:
        return {"channel_version": _PROTOCOL_VERSION, "bot_agent": "Ophira/0.1.0"}

    async def start_login(self) -> WeixinQrCode:
        payload = await self._request(
            "POST", "/ilink/bot/get_bot_qrcode?bot_type=3", body={"local_token_list": []},
        )
        _check_api_result(payload, require_success=False)
        return WeixinQrCode(
            qrcode=_string(payload.get("qrcode"), maximum=8192, error="MALFORMED_RESPONSE"),
            qrcode_img_content=_string(
                payload.get("qrcode_img_content"), maximum=16384, error="MALFORMED_RESPONSE",
            ),
        )

    async def login_status(
        self, qrcode: str, *, base_url: str = WEIXIN_API_ORIGIN,
        verify_code: str | None = None,
    ) -> WeixinLoginStatus:
        params = {"qrcode": _string(qrcode, maximum=8192)}
        if verify_code is not None:
            params["verify_code"] = _string(verify_code, maximum=64)
        payload = await self._request(
            "GET", "/ilink/bot/get_qrcode_status?" + urlencode(params),
            base_url=base_url, poll=True,
        )
        _check_api_result(payload, require_success=False)
        status = payload.get("status")
        if not isinstance(status, str) or status not in _LOGIN_STATES:
            raise WeixinTransportError("MALFORMED_RESPONSE")
        received_base = payload.get("baseurl")
        checked_base = (validate_weixin_base_url(received_base)
                        if received_base is not None else None)
        redirect = payload.get("redirect_host")
        if redirect is not None:
            if not isinstance(redirect, str) or redirect not in _API_HOSTS:
                raise WeixinTransportError("DESTINATION_REJECTED")
        if status == "scaned_but_redirect" and redirect is None:
            raise WeixinTransportError("MALFORMED_RESPONSE")
        token = _optional_string(payload.get("bot_token"), 8192)
        bot_id = _optional_string(payload.get("ilink_bot_id"), 256)
        user_id = _optional_string(payload.get("ilink_user_id"), 256)
        if status == "confirmed":
            if not token or not bot_id or not user_id:
                raise WeixinTransportError("MALFORMED_RESPONSE")
            checked_base = checked_base or validate_weixin_base_url(base_url)
        return WeixinLoginStatus(
            status=status, bot_token=token, ilink_bot_id=bot_id, ilink_user_id=user_id,
            base_url=checked_base, redirect_host=redirect,
        )

    async def get_updates(
        self, token: str, *, cursor: str = "", base_url: str = WEIXIN_API_ORIGIN,
    ) -> WeixinUpdates:
        _string(cursor, maximum=65536, allow_empty=True)
        payload = await self._request(
            "POST", "/ilink/bot/getupdates", token=token, base_url=base_url, poll=True,
            body={"get_updates_buf": cursor, "base_info": self._base_info()},
        )
        # Official polling responses make ret optional. An empty successful poll
        # may omit default-valued fields; it must not invent cursor progress.
        _check_api_result(payload, require_success=False)
        msgs = payload.get("msgs", [])
        if not isinstance(msgs, list) or any(not isinstance(item, dict) for item in msgs):
            raise WeixinTransportError("MALFORMED_RESPONSE")
        if len(msgs) > MAX_WEIXIN_MESSAGES:
            raise WeixinTransportError("RESPONSE_TOO_LARGE")
        next_cursor = payload.get("get_updates_buf")
        if next_cursor is not None:
            _string(next_cursor, maximum=65536, allow_empty=True, error="MALFORMED_RESPONSE")
        timeout_ms = payload.get("longpolling_timeout_ms")
        if timeout_ms is not None and (
            not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= 600000
        ):
            raise WeixinTransportError("MALFORMED_RESPONSE")
        return WeixinUpdates(tuple(msgs), next_cursor, timeout_ms)

    async def send_text(
        self, token: str, *, to_user_id: str, text: str, client_id: str, context_token: str,
        base_url: str = WEIXIN_API_ORIGIN, run_id: str | None = None,
    ) -> WeixinSendResult:
        _string(to_user_id, maximum=256)
        if (
            not to_user_id.isascii() or to_user_id.count("@") != 1
            or not to_user_id.endswith("@im.wechat") or to_user_id == "@im.wechat"
            or any(c.isspace() for c in to_user_id)
        ):
            raise WeixinTransportError("INVALID_ARGUMENT")
        _string(text, maximum=MAX_WEIXIN_TEXT_CHARS, multiline=True)
        _string(client_id, maximum=256)
        _string(context_token, maximum=8192)
        msg: dict[str, Any] = {
            "from_user_id": "", "to_user_id": to_user_id, "client_id": client_id,
            "message_type": 2, "message_state": 2, "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if run_id is not None:
            msg["run_id"] = _string(run_id, maximum=256)
        payload = await self._request(
            "POST", "/ilink/bot/sendmessage", base_url=base_url, token=token,
            body={"msg": msg, "base_info": self._base_info()}, is_send=True,
        )
        try:
            _check_api_result(payload, require_success=True)
        except WeixinTransportError as exc:
            raise WeixinTransportError(
                exc.code, outcome_unknown=exc.code == "MALFORMED_RESPONSE",
            ) from None
        return WeixinSendResult(_message_id(payload.get("message_id")), client_id)
