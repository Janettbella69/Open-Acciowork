"""Authenticated WeCom intelligent bot ingress into the ordinary Run path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .auth import AuthorizationError
from .channel_delivery import ChannelDeliveryService, WeComReplyTarget
from .channel_identity import (
    ChannelIdentityBindingStore,
    ChannelIdentityError,
    ChannelIdentitySelector,
)
from .channels import (
    CHANNEL_USER_REQUEST_CLOSE,
    CHANNEL_USER_REQUEST_NOTICE,
    CHANNEL_USER_REQUEST_OPEN,
    ChannelError,
    ChannelReplayGuard,
)
from .config import Settings
from .models import ChannelInbound
from .policy import OrgPolicyStore
from .runs import RunManager
from .untrusted import UntrustedSource, sanitize_untrusted_text
from .wecom_delivery import WeComReplyAdapter

logger = logging.getLogger(__name__)
WECOM_MESSAGE_COMMAND = "aibot_msg_callback"


class _SafeSDKLogger:
    """Suppress raw SDK frames and provider error text, which may contain secrets."""

    def debug(self, _message: str, *args: object) -> None:
        pass

    def info(self, _message: str, *args: object) -> None:
        pass

    def warn(self, _message: str, *args: object) -> None:
        logger.warning("WeCom SDK reported a warning")

    def error(self, _message: str, *args: object) -> None:
        logger.error("WeCom SDK reported an error")


def parse_wecom_frame(
    frame: dict[str, Any], *, bot_id: str,
) -> tuple[ChannelIdentitySelector, WeComReplyTarget, str] | None:
    if not isinstance(frame, dict) or frame.get("cmd") != WECOM_MESSAGE_COMMAND:
        raise ChannelError("INVALID_FRAME", "WeCom message frame has an invalid command")
    body = frame.get("body")
    if not isinstance(body, dict):
        raise ChannelError("INVALID_FRAME", "WeCom message frame has no body")
    if body.get("aibotid") != bot_id:
        raise ChannelError("BOT_SCOPE_MISMATCH", "WeCom bot ID did not match this connection")
    if body.get("msgtype") != "text":
        return None
    chat_type = body.get("chattype")
    if chat_type not in {"group", "single"}:
        raise ChannelError("INVALID_FRAME", "WeCom conversation type is invalid")
    sender = body.get("from")
    content = body.get("text")
    if not isinstance(sender, dict) or not isinstance(content, dict):
        raise ChannelError("INVALID_FRAME", "WeCom sender or text is missing")
    if not isinstance(content.get("content"), str):
        raise ChannelError("INVALID_FRAME", "WeCom text content is missing")
    user_id = sender.get("userid")
    conversation_id = body.get("chatid") if chat_type == "group" else user_id
    try:
        selector = ChannelIdentitySelector(
            channel="wecom",
            tenant_key=body.get("aibotid"),
            chat_id=f"{chat_type}:{conversation_id}",
            user_open_id=user_id,
        )
        target = WeComReplyTarget(
            event_id=body.get("msgid"),
            message_id=body.get("msgid"),
            chat_id=conversation_id,
            chat_type=chat_type,
            sender_user_id=selector.user_open_id,
            bot_id=bot_id,
        )
    except ValueError as exc:
        raise ChannelError("INVALID_FRAME", "WeCom sender or conversation ID is invalid") from exc
    sanitized = sanitize_untrusted_text(content["content"], UntrustedSource.CHANNEL_MESSAGE)
    text = " ".join(sanitized.text.split())
    if not text:
        raise ChannelError("EMPTY", "WeCom message had no text")
    request_text = (
        f"{CHANNEL_USER_REQUEST_NOTICE}\n{CHANNEL_USER_REQUEST_OPEN}\n"
        f"{text}\n{CHANNEL_USER_REQUEST_CLOSE}"
    )
    return selector, target, request_text


class WeComChannel:
    """Official SDK lifecycle and Run dispatch for WeCom bot messages."""

    def __init__(
        self,
        settings: Settings,
        bindings: ChannelIdentityBindingStore,
        replays: ChannelReplayGuard,
        delivery: ChannelDeliveryService,
        adapter: WeComReplyAdapter,
        runs: RunManager,
        policy: OrgPolicyStore,
    ) -> None:
        self.settings = settings
        self.bindings = bindings
        self.replays = replays
        self.delivery = delivery
        self.adapter = adapter
        self.runs = runs
        self.policy = policy
        self._client: Any = None
        self._connect_task: asyncio.Task[Any] | None = None

    @property
    def configured(self) -> bool:
        return self.delivery.configured_for("wecom")

    async def dispatch_authenticated_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Called exclusively after the official SDK authenticates the WebSocket."""

        if not self.adapter.ready:
            raise ChannelError("CHANNEL_OFFLINE", "WeCom connection is not authenticated")
        parsed = parse_wecom_frame(frame, bot_id=self.settings.wecom_bot_id or "")
        if parsed is None:
            return {"ignored": True, "channel": "wecom"}
        selector, target, request_text = parsed
        resolved = self.bindings.resolve(selector)
        if not self.policy.get().channel_delivery_enabled:
            raise ChannelError("CHANNEL_POLICY_DISABLED", "WeCom channel delivery is disabled")
        identity = (
            f"wecom:business:v1\0{selector.tenant_key}\0{selector.chat_id}\0"
            f"{selector.user_open_id}\0{target.message_id}"
        )
        if self.replays.claim(identity) is None:
            return {"duplicate": True, "channel": "wecom"}
        inbound = ChannelInbound(
            channel="wecom", text=request_text,
            project_id=resolved.binding.project_id,
            sender=selector.user_open_id[:160],
        )
        run = await self.runs.create_and_start(
            resolved.binding.project_id,
            inbound.text,
            channel_reply_target=target,
            principal=resolved.principal,
        )
        obligation = self.delivery.store.get_for_run(run.id)
        if obligation is None:
            raise RuntimeError("WeCom delivery obligation was not persisted")
        return {
            "run_id": run.id,
            "project_id": run.project_id,
            "channel": inbound.channel,
            "delivery_id": obligation.id,
            "delivery_status": obligation.status.value,
        }

    async def start(self) -> None:
        if not self.configured:
            return
        if self._client is not None:
            return
        try:
            from aibot import WSClient, WSClientOptions
        except ImportError as exc:
            raise RuntimeError(
                "WeCom channel requires the backend[wecom] optional dependency"
            ) from exc
        secret = self.settings.wecom_bot_secret
        assert secret is not None  # guarded by configured
        client = WSClient(WSClientOptions(
            bot_id=self.settings.wecom_bot_id or "",
            secret=secret.get_secret_value(),
            max_reconnect_attempts=-1,
            logger=_SafeSDKLogger(),
        ))
        client.on("authenticated", self.adapter.authenticated)
        client.on("disconnected", lambda _reason: self.adapter.disconnected())

        async def on_text(frame: dict[str, Any]) -> None:
            try:
                await self.dispatch_authenticated_frame(frame)
            except (ChannelError, ChannelIdentityError, AuthorizationError) as exc:
                logger.warning("WeCom callback rejected: %s", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - SDK callback must not expose content
                logger.error("WeCom callback dispatch failed: %s", type(exc).__name__)

        client.on("message.text", on_text)
        self.adapter.attach(client)
        self._client = client
        self._connect_task = asyncio.create_task(client.connect(), name="ophira:wecom-bot")

    async def stop(self) -> None:
        self.adapter.disconnected()
        if self._client is not None:
            self._client.disconnect()
        if self._connect_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._connect_task, return_exceptions=True),
                    timeout=15,
                )
            except TimeoutError:
                self._connect_task.cancel()
                await asyncio.gather(self._connect_task, return_exceptions=True)
        self.adapter.detach()
        self._client = None
        self._connect_task = None
