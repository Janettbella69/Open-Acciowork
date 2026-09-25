"""DingTalk robot OpenAPI reply adapter for authenticated Stream messages."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from .channel_delivery import (
    MAX_FEISHU_RESPONSE_BYTES,
    ChannelDeliveryError,
    ChannelReplyResult,
    DingTalkReplyTarget,
    _bounded_reply,
)
from .config import Settings

DINGTALK_OPENAPI_ORIGIN = "https://api.dingtalk.com"


class DingTalkReplyAdapter:
    """Send a group or direct reply with an app token; never retry an ambiguous send."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_key = settings.dingtalk_app_key or ""
        self._app_secret = (
            settings.dingtalk_app_secret.get_secret_value()
            if settings.dingtalk_app_secret is not None
            else ""
        )
        self._robot_code = settings.dingtalk_robot_code or ""
        self._timeout = settings.dingtalk_delivery_timeout_seconds
        self._transport = transport
        self._token: str | None = None
        self._token_expires_monotonic = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._app_key and self._app_secret and self._robot_code)

    async def _request(
        self,
        path: str,
        body: dict[str, Any],
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        is_send = token is not None
        try:
            async with httpx.AsyncClient(
                base_url=DINGTALK_OPENAPI_ORIGIN,
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    path,
                    json=body,
                    headers={"x-acs-dingtalk-access-token": token} if token else None,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ChannelDeliveryError(
                "NETWORK_ERROR",
                "DingTalk request ended without a confirmed outcome.",
                retryable=not is_send,
            ) from exc
        if len(response.content) > MAX_FEISHU_RESPONSE_BYTES:
            raise ChannelDeliveryError(
                "RESPONSE_TOO_LARGE", "DingTalk response exceeded its size limit.",
                retryable=False,
            )
        if not 200 <= response.status_code < 300:
            raise ChannelDeliveryError(
                f"HTTP_{response.status_code}",
                "DingTalk did not confirm the request.",
                retryable=not is_send and (
                    response.status_code == 429 or response.status_code >= 500
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChannelDeliveryError(
                "MALFORMED_RESPONSE", "DingTalk returned a malformed response.",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise ChannelDeliveryError(
                "MALFORMED_RESPONSE", "DingTalk response was not an object.",
                retryable=False,
            )
        return payload

    async def _access_token(self) -> str:
        if not self.configured:
            raise ChannelDeliveryError(
                "CHANNEL_NOT_CONFIGURED", "DingTalk app credentials are not configured.",
                retryable=False,
            )
        current = time.monotonic()
        if self._token and current < self._token_expires_monotonic:
            return self._token
        async with self._token_lock:
            current = time.monotonic()
            if self._token and current < self._token_expires_monotonic:
                return self._token
            payload = await self._request(
                "/v1.0/oauth2/accessToken",
                {"appKey": self._app_key, "appSecret": self._app_secret},
            )
            token = payload.get("accessToken")
            expires = payload.get("expireIn")
            if (
                not isinstance(token, str) or not token
                or not isinstance(expires, int) or isinstance(expires, bool)
                or expires <= 0
            ):
                raise ChannelDeliveryError(
                    "MALFORMED_TOKEN_RESPONSE",
                    "DingTalk access-token response omitted required fields.",
                    retryable=False,
                )
            margin = min(300, max(1, expires // 10))
            self._token = token
            self._token_expires_monotonic = current + max(1, expires - margin)
            return token

    async def reply(
        self,
        target: DingTalkReplyTarget,
        text: str,
        *,
        idempotency_key: str,
    ) -> ChannelReplyResult:
        # The ledger retains this stable key for correlation. DingTalk's robot
        # send APIs do not document a deduplication field, so it is not sent.
        _ = idempotency_key
        if target.robot_code != self._robot_code:
            raise ChannelDeliveryError(
                "ROBOT_SCOPE_MISMATCH", "DingTalk robot code changed after ingress.",
                retryable=False,
            )
        token = await self._access_token()
        body: dict[str, Any] = {
            "robotCode": target.robot_code,
            "msgKey": "sampleText",
            "msgParam": json.dumps(
                {"content": _bounded_reply(text)}, ensure_ascii=False, separators=(",", ":"),
            ),
        }
        if target.chat_type == "group":
            path = "/v1.0/robot/groupMessages/send"
            body["openConversationId"] = target.chat_id
        else:
            path = "/v1.0/robot/oToMessages/batchSend"
            body["userIds"] = [target.sender_staff_id]
        payload = await self._request(path, body, token=token)
        process_key = payload.get("processQueryKey")
        if not isinstance(process_key, str) or not process_key or len(process_key) > 200:
            raise ChannelDeliveryError(
                "MALFORMED_SEND_RESPONSE",
                "DingTalk did not return a message tracking key.",
                retryable=False,
            )
        if target.chat_type == "p2p" and (
            payload.get("invalidStaffIdList") or payload.get("flowControlledStaffIdList")
        ):
            raise ChannelDeliveryError(
                "RECIPIENT_NOT_ACCEPTED",
                "DingTalk did not accept the direct-message recipient.",
                retryable=False,
            )
        return ChannelReplyResult(message_id=process_key, chat_id=target.chat_id)
