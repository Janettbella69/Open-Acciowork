"""Authenticated DingTalk Stream ingress into the ordinary OPHIRA Run path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .auth import AuthorizationError
from .channel_delivery import ChannelDeliveryService, DingTalkReplyTarget
from .channel_identity import (
    ChannelIdentityBindingStore,
    ChannelIdentityError,
    ChannelIdentityIntegrityError,
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

logger = logging.getLogger(__name__)
DINGTALK_BOT_TOPIC = "/v1.0/im/bot/messages/get"


def parse_dingtalk_message(
    data: dict[str, Any],
    *,
    robot_code: str,
) -> tuple[ChannelIdentitySelector, DingTalkReplyTarget, str] | None:
    """Read only SDK-authenticated callback fields; ignore unsupported/ambient chat."""

    if not isinstance(data, dict):
        raise ChannelError("INVALID_CALLBACK", "DingTalk callback data must be an object")
    if data.get("robotCode") != robot_code:
        raise ChannelError("ROBOT_SCOPE_MISMATCH", "DingTalk robot code did not match this app")
    conversation_type = data.get("conversationType")
    if conversation_type not in {"1", "2"}:
        return None
    if conversation_type == "2" and data.get("isInAtList") is not True:
        return None
    if data.get("msgtype") != "text":
        return None
    content = data.get("text")
    if not isinstance(content, dict) or not isinstance(content.get("content"), str):
        raise ChannelError("INVALID_CALLBACK", "DingTalk text callback is incomplete")
    try:
        selector = ChannelIdentitySelector(
            channel="dingtalk",
            tenant_key=data.get("chatbotCorpId"),
            chat_id=data.get("conversationId"),
            user_open_id=data.get("senderStaffId"),
        )
        target = DingTalkReplyTarget(
            event_id=data.get("msgId"),
            message_id=data.get("msgId"),
            chat_id=selector.chat_id,
            chat_type="group" if conversation_type == "2" else "p2p",
            sender_staff_id=selector.user_open_id,
            robot_code=robot_code,
        )
    except ValueError as exc:
        raise ChannelError(
            "INVALID_CALLBACK", "DingTalk sender or conversation ID is invalid",
        ) from exc
    sanitized = sanitize_untrusted_text(content["content"], UntrustedSource.CHANNEL_MESSAGE)
    text = " ".join(sanitized.text.split())
    if not text:
        raise ChannelError("EMPTY", "DingTalk message had no text")
    request_text = (
        f"{CHANNEL_USER_REQUEST_NOTICE}\n{CHANNEL_USER_REQUEST_OPEN}\n"
        f"{text}\n{CHANNEL_USER_REQUEST_CLOSE}"
    )
    return selector, target, request_text


class DingTalkChannel:
    """Official Stream client lifecycle and verified callback dispatch."""

    def __init__(
        self,
        settings: Settings,
        bindings: ChannelIdentityBindingStore,
        replays: ChannelReplayGuard,
        delivery: ChannelDeliveryService,
        runs: RunManager,
        policy: OrgPolicyStore,
    ) -> None:
        self.settings = settings
        self.bindings = bindings
        self.replays = replays
        self.delivery = delivery
        self.runs = runs
        self.policy = policy
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return self.delivery.configured_for("dingtalk")

    async def dispatch_authenticated_callback(
        self,
        data: dict[str, Any],
        *,
        topic: str,
    ) -> dict[str, Any]:
        """Called exclusively by the registered official SDK Stream handler."""

        if topic != DINGTALK_BOT_TOPIC:
            raise ChannelError("INVALID_TOPIC", "DingTalk callback topic is invalid")
        if not self.configured:
            raise ChannelError("CHANNEL_NOT_CONFIGURED", "DingTalk channel is not configured")
        parsed = parse_dingtalk_message(
            data, robot_code=self.settings.dingtalk_robot_code or "",
        )
        if parsed is None:
            return {"ignored": True, "channel": "dingtalk"}
        selector, target, request_text = parsed
        resolved = self.bindings.resolve(selector)
        if not self.policy.get().channel_delivery_enabled:
            raise ChannelError("CHANNEL_POLICY_DISABLED", "DingTalk channel delivery is disabled")
        identity = (
            f"dingtalk:business:v1\0{selector.tenant_key}\0{selector.chat_id}\0"
            f"{selector.user_open_id}\0{target.message_id}"
        )
        if self.replays.claim(identity) is None:
            return {"duplicate": True, "channel": "dingtalk"}
        inbound = ChannelInbound(
            channel="dingtalk",
            text=request_text,
            project_id=resolved.binding.project_id,
            sender=selector.user_open_id[:160],
        )
        # Keep the replay claim after any ambiguous Run creation failure. A
        # callback retry must never launch a second Run for one DingTalk msgId.
        run = await self.runs.create_and_start(
            resolved.binding.project_id,
            inbound.text,
            channel_reply_target=target,
            principal=resolved.principal,
        )
        obligation = self.delivery.store.get_for_run(run.id)
        if obligation is None:
            raise RuntimeError("DingTalk delivery obligation was not persisted")
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
        if self._task is not None and not self._task.done():
            return
        try:
            import dingtalk_stream
        except ImportError as exc:
            raise RuntimeError(
                "DingTalk channel requires the backend[dingtalk] optional dependency"
            ) from exc

        channel = self

        class RunHandler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: Any) -> tuple[int, str]:
                try:
                    await channel.dispatch_authenticated_callback(
                        callback.data,
                        topic=callback.headers.topic,
                    )
                except ChannelError as exc:
                    status = (
                        dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION
                        if exc.code.startswith("REPLAY_") or exc.code == "CHANNEL_NOT_CONFIGURED"
                        else dingtalk_stream.AckMessage.STATUS_BAD_REQUEST
                    )
                    logger.warning("DingTalk callback rejected: %s", type(exc).__name__)
                    return (
                        status,
                        "unavailable"
                        if status == dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION
                        else "rejected",
                    )
                except ChannelIdentityIntegrityError:
                    logger.error("DingTalk identity binding store is unavailable")
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, "unavailable"
                except (ChannelIdentityError, AuthorizationError) as exc:
                    logger.warning("DingTalk callback rejected: %s", type(exc).__name__)
                    return dingtalk_stream.AckMessage.STATUS_BAD_REQUEST, "rejected"
                except Exception as exc:  # noqa: BLE001 - SDK must receive a bounded ACK
                    logger.error("DingTalk callback dispatch failed: %s", type(exc).__name__)
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, "unavailable"
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        secret = self.settings.dingtalk_app_secret
        assert secret is not None  # guarded by configured
        client = dingtalk_stream.DingTalkStreamClient(
            dingtalk_stream.Credential(
                self.settings.dingtalk_app_key,
                secret.get_secret_value(),
            ),
        )
        client.register_callback_handler(DINGTALK_BOT_TOPIC, RunHandler())
        self._client = client
        self._task = asyncio.create_task(client.start(), name="ophira:dingtalk-stream")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._client = None
        self._task = None
