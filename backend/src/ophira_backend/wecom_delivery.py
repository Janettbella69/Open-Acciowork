"""Durable WeCom intelligent bot reply adapter over the official SDK client."""

from __future__ import annotations

from typing import Any

from .channel_delivery import (
    ChannelDeliveryError,
    ChannelReplyResult,
    WeComReplyTarget,
    _bounded_reply,
)
from .config import Settings


class WeComReplyAdapter:
    """Send only while authenticated; never replay an ambiguous WebSocket send."""

    def __init__(self, settings: Settings) -> None:
        self._bot_id = settings.wecom_bot_id or ""
        self._bot_secret_configured = settings.wecom_bot_secret is not None
        self._client: Any = None
        self._authenticated = False

    @property
    def configured(self) -> bool:
        return bool(self._bot_id and self._bot_secret_configured)

    @property
    def ready(self) -> bool:
        return bool(
            self._authenticated and self._client is not None and self._client.is_connected
        )

    def attach(self, client: Any) -> None:
        self._client = client
        self._authenticated = False

    def authenticated(self) -> None:
        self._authenticated = True

    def disconnected(self) -> None:
        self._authenticated = False

    def detach(self) -> None:
        self._authenticated = False
        self._client = None

    async def reply(
        self,
        target: WeComReplyTarget,
        text: str,
        *,
        idempotency_key: str,
    ) -> ChannelReplyResult:
        # The stable ledger key is for correlation. The SDK's active-send
        # command generates a new req_id and documents no idempotency field.
        _ = idempotency_key
        if target.bot_id != self._bot_id:
            raise ChannelDeliveryError(
                "BOT_SCOPE_MISMATCH", "WeCom bot changed after ingress.", retryable=False,
            )
        client = self._client
        if not self.configured or client is None:
            raise ChannelDeliveryError(
                "CHANNEL_NOT_CONFIGURED", "WeCom bot is not configured.", retryable=False,
            )
        if not self.ready:
            # No send was attempted; it is safe to wait for authentication.
            raise ChannelDeliveryError(
                "CHANNEL_OFFLINE", "WeCom bot is waiting for an authenticated connection.",
                retryable=True,
            )
        recipient = target.chat_id if target.chat_type == "group" else target.sender_user_id
        try:
            ack = await client.send_message(
                recipient,
                {"msgtype": "markdown", "markdown": {"content": _bounded_reply(text)}},
            )
        except Exception as exc:  # noqa: BLE001 - SDK may have sent before reporting failure
            raise ChannelDeliveryError(
                "UNCERTAIN_OUTCOME", "WeCom send ended without a confirmed acknowledgement.",
                retryable=False,
            ) from exc
        if not isinstance(ack, dict) or ack.get("errcode") != 0:
            raise ChannelDeliveryError(
                "MALFORMED_ACK", "WeCom did not confirm the outbound message.",
                retryable=False,
            )
        headers = ack.get("headers")
        req_id = headers.get("req_id") if isinstance(headers, dict) else None
        if not isinstance(req_id, str) or not req_id or len(req_id) > 200:
            raise ChannelDeliveryError(
                "MALFORMED_ACK", "WeCom acknowledgement omitted its request ID.",
                retryable=False,
            )
        return ChannelReplyResult(message_id=req_id, chat_id=recipient)
