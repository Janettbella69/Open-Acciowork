"""Weixin single-chat replies through a private context and account vault."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .channel_delivery import (
    ChannelDeliveryError,
    ChannelReplyResult,
    WeixinReplyTarget,
    _bounded_reply,
)
from .weixin_transport import WeixinTransport, WeixinTransportError

if TYPE_CHECKING:
    from .weixin_accounts import WeixinStore


MAX_WEIXIN_REPLY_CHARS = 4_000
_TRUNCATION_NOTICE = "\n\n（内容较长，完整结果请在工作台查看。）"


class WeixinReplyAdapter:
    """Revalidate the frozen binding and send one bounded, contextual reply."""

    def __init__(self, store: WeixinStore, transport: WeixinTransport) -> None:
        self.store = store
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.store.active_accounts())

    async def reply(
        self, target: WeixinReplyTarget, text: str, *, idempotency_key: str,
    ) -> ChannelReplyResult:
        try:
            account, _principal = self.store.resolve(
                target.account_id, target.sender_user_id,
                expected_revision=target.account_revision,
            )
            if account.bot_id != target.bot_id or account.owner_user_id != target.chat_id:
                raise ValueError("reply scope changed")
            token = self.store.credentials(
                target.account_id, expected_revision=target.account_revision,
            )
            context = self.store.reply_context(
                target.account_id, target.context_ref,
                expected_revision=target.account_revision,
            )
        except Exception as exc:
            raise ChannelDeliveryError(
                "REPLY_SCOPE_UNAVAILABLE", "Weixin reply authorization or context is unavailable.",
                retryable=False,
            ) from exc
        content = _bounded_reply(text)
        if len(content) > MAX_WEIXIN_REPLY_CHARS:
            content = (
                content[:MAX_WEIXIN_REPLY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
            )
        try:
            result = await self.transport.send_text(
                token, to_user_id=target.sender_user_id, text=content,
                client_id=idempotency_key, context_token=context, base_url=account.base_url,
            )
        except WeixinTransportError as exc:
            try:
                self.store.set_error(
                    target.account_id, exc.code, expected_revision=target.account_revision,
                )
            except Exception:
                pass  # A changed binding must not receive an old send's diagnostic.
            # An acknowledgement can be lost after the platform accepts a send.
            # A stable client_id is correlation, not a documented deduplication
            # guarantee, so every uncertain send remains terminal.
            raise ChannelDeliveryError(
                "UNCERTAIN_OUTCOME" if exc.outcome_unknown else exc.code,
                "Weixin did not confirm this reply.",
                retryable=exc.retryable and not exc.outcome_unknown,
            ) from exc
        return ChannelReplyResult(
            message_id=result.message_id or f"client:{result.client_id}",
            chat_id=target.sender_user_id,
        )
