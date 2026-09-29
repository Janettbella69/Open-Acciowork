"""Official Weixin single-chat polling into the ordinary RunManager path.

The durable cursor is committed after a whole batch. Run claim IDs survive
replay-cache expiry; an ambiguous creation keeps its replay claim and surfaces
a safe account error instead of risking another billable Run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .channel_delivery import ChannelDeliveryService, WeixinReplyTarget
from .channels import (
    CHANNEL_USER_REQUEST_CLOSE,
    CHANNEL_USER_REQUEST_NOTICE,
    CHANNEL_USER_REQUEST_OPEN,
    ChannelError,
    ChannelReplayGuard,
)
from .policy import OrgPolicyStore
from .runs import RunManager
from .untrusted import UntrustedSource, sanitize_untrusted_text
from .weixin_transport import WeixinTransport, WeixinTransportError

if TYPE_CHECKING:
    from .weixin_accounts import WeixinStore

logger = logging.getLogger(__name__)
MAX_CURSOR_CHARS = 65_536
MAX_CONTEXT_CHARS = 8_192
POLL_RETRY_SECONDS = 3


@dataclass(frozen=True)
class WeixinInboundMessage:
    message_id: str
    sender_user_id: str
    request_text: str
    context_token: str = field(repr=False)


def parse_weixin_message(
    message: dict[str, Any], *, bot_id: str, owner_user_id: str,
    enabled_at: datetime | None,
) -> WeixinInboundMessage:
    """Validate a completed owner text message from an authenticated poll."""
    if not isinstance(message, dict):
        raise ChannelError("INVALID_MESSAGE", "Weixin message must be an object")
    if message.get("group_id") not in (None, ""):
        raise ChannelError("GROUP_UNSUPPORTED", "Weixin group messages are unsupported")
    if (type(message.get("message_type")) is not int or message["message_type"] != 1
            or type(message.get("message_state")) is not int or message["message_state"] != 2):
        raise ChannelError("MESSAGE_UNSUPPORTED", "Only completed user messages are accepted")
    sender = message.get("from_user_id")
    if (not isinstance(sender, str) or sender != owner_user_id
            or not sender.endswith("@im.wechat") or len(sender) > 256):
        raise ChannelError("SENDER_REJECTED", "Weixin sender is not the paired owner")
    recipient = message.get("to_user_id")
    if recipient not in (None, "") and recipient != bot_id:
        raise ChannelError("RECIPIENT_REJECTED", "Weixin bot recipient does not match")
    raw_id = message.get("message_id")
    if type(raw_id) is int:
        message_id = str(raw_id)
    elif isinstance(raw_id, str) and re.fullmatch(r"[0-9]{1,20}", raw_id):
        message_id = str(int(raw_id))
    else:
        raise ChannelError("INVALID_MESSAGE_ID", "Weixin message ID is invalid")
    if not 0 <= int(message_id) <= 2**64 - 1:
        raise ChannelError("INVALID_MESSAGE_ID", "Weixin message ID is outside uint64")
    created = message.get("create_time_ms")
    if (type(created) is not int or not 0 <= created <= 2**64 - 1
            or enabled_at is None or enabled_at.tzinfo is None):
        raise ChannelError("INVALID_MESSAGE_TIME", "Weixin message time is unavailable")
    if created < math.ceil(enabled_at.timestamp() * 1000):
        raise ChannelError("STALE_MESSAGE", "Weixin message predates the current binding")
    context = message.get("context_token")
    if not isinstance(context, str) or not context.strip() or len(context) > MAX_CONTEXT_CHARS:
        raise ChannelError("REPLY_CONTEXT_MISSING", "Weixin reply context is unavailable")
    items = message.get("item_list")
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or type(items[0].get("type")) is not int or items[0]["type"] != 1
            or any(key.endswith("_item") and key != "text_item" for key in items[0])):
        raise ChannelError("TEXT_REQUIRED", "Only a single Weixin text item is supported")
    text_item = items[0].get("text_item")
    if not isinstance(text_item, dict) or not isinstance(text_item.get("text"), str):
        raise ChannelError("TEXT_REQUIRED", "Weixin text item is invalid")
    sanitized = sanitize_untrusted_text(text_item["text"], UntrustedSource.CHANNEL_MESSAGE)
    text = sanitized.text.strip()
    if not text:
        raise ChannelError("EMPTY", "Weixin message had no text")
    request = (
        f"{CHANNEL_USER_REQUEST_NOTICE}\n{CHANNEL_USER_REQUEST_OPEN}\n"
        f"{text}\n{CHANNEL_USER_REQUEST_CLOSE}"
    )
    return WeixinInboundMessage(message_id, sender, request, context)


def _claim_id(account_id: str, revision: int, message_id: str) -> str:
    raw = f"{account_id}\0{revision}\0{message_id}"
    return "weixin_" + hashlib.sha256(raw.encode()).hexdigest()


class WeixinChannel:
    """One leased long poll per account; new accounts need no process restart."""

    def __init__(
        self, store: WeixinStore, transport: WeixinTransport, policy: OrgPolicyStore,
        replays: ChannelReplayGuard, runs: RunManager, delivery: ChannelDeliveryService,
    ) -> None:
        self.store = store
        self.transport = transport
        self.policy = policy
        self.replays = replays
        self.runs = runs
        self.delivery = delivery
        self._supervisor: asyncio.Task[None] | None = None
        self._pollers: dict[str, tuple[int, asyncio.Task[None]]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.store.active_accounts())

    def _record_error(self, account_id: str, code: str, revision: int) -> None:
        try:
            self.store.set_error(account_id, code, expected_revision=revision)
        except Exception:
            # Rebinding can invalidate the observation. Never log remote frames,
            # response bodies, credential/context tokens, or exception messages.
            logger.warning("Weixin account diagnostic could not be recorded")

    async def dispatch_authenticated_message(
        self, account_id: str, message: dict[str, Any], *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Internal transport callback; it is deliberately not an HTTP ingress."""
        account = self.store.get_account(account_id)
        revision = account.revision if expected_revision is None else expected_revision
        account, _ = self.store.resolve(
            account_id, account.owner_user_id, expected_revision=revision,
        )
        parsed = parse_weixin_message(
            message, bot_id=account.bot_id, owner_user_id=account.owner_user_id,
            enabled_at=account.enabled_at,
        )
        if not self.policy.get().channel_delivery_enabled:
            raise ChannelError("CHANNEL_POLICY_DISABLED", "Weixin channel delivery is disabled")
        claim_id = _claim_id(account_id, revision, parsed.message_id)
        with self.runs.ledger.lifecycle.lease(account.project_id):
            for previous in self.runs.ledger.iter_runs(account.project_id):
                if previous.automation_claim_id == claim_id:
                    return {"duplicate": True, "channel": "weixin", "run_id": previous.id}
            if self.replays.claim(claim_id) is None:
                return {"duplicate": True, "channel": "weixin"}
            try:
                context_ref = self.store.save_context(
                    account_id, parsed.message_id, parsed.context_token,
                    expected_revision=revision,
                )
                target = WeixinReplyTarget(
                    account_id=account_id, account_revision=revision, bot_id=account.bot_id,
                    event_id=parsed.message_id, message_id=parsed.message_id,
                    chat_id=parsed.sender_user_id, sender_user_id=parsed.sender_user_id,
                    context_ref=context_ref,
                )
                # Recheck after durable context/replay writes, immediately before
                # entering RunManager's own authorization and lifecycle gates.
                account, principal = self.store.resolve(
                    account_id, parsed.sender_user_id, expected_revision=revision,
                )
                run = await self.runs.create_and_start(
                    account.project_id, parsed.request_text, principal=principal,
                    automation_claim_id=claim_id, channel_reply_target=target,
                )
                obligation = self.delivery.store.get_for_run(run.id)
                if obligation is None:
                    raise RuntimeError("Weixin reply obligation was not persisted")
            except asyncio.CancelledError:
                self._record_error(account_id, "INGRESS_RUN_UNCERTAIN", revision)
                raise
            except Exception as exc:
                self._record_error(account_id, "INGRESS_RUN_UNCERTAIN", revision)
                raise ChannelError(
                    "INGRESS_RUN_UNCERTAIN", "Weixin Run creation could not be confirmed",
                ) from exc
        return {
            "channel": "weixin", "run_id": run.id, "project_id": run.project_id,
            "delivery_id": obligation.id, "delivery_status": obligation.status.value,
        }

    async def poll_once(self, account_id: str) -> dict[str, Any]:
        if not self.policy.get().channel_delivery_enabled:
            return {"paused": True, "processed": 0}
        with self.store.poll_lease(account_id) as acquired:
            if not acquired:
                return {"busy": True, "processed": 0}
            account = self.store.get_account(account_id)
            revision = account.revision
            account, _ = self.store.resolve(
                account_id, account.owner_user_id, expected_revision=revision,
            )
            state = self.store.get_poll_state(account_id)
            cursor = state.get("cursor", "")
            if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_CHARS:
                raise ChannelError("POLL_STATE_INVALID", "Weixin poll cursor is invalid")
            token = self.store.credentials(account_id, expected_revision=revision)
            try:
                batch = await self.transport.get_updates(
                    token, cursor=cursor, base_url=account.base_url,
                )
            except WeixinTransportError as exc:
                self._record_error(account_id, exc.code, revision)
                raise
            # In-flight results from disabled/rebound accounts cannot create Runs
            # or overwrite the replacement binding's cursor.
            self.store.resolve(account_id, account.owner_user_id, expected_revision=revision)
            if not self.policy.get().channel_delivery_enabled:
                return {"paused": True, "processed": 0}
            next_cursor = batch.get_updates_buf
            if next_cursor is not None and (
                not isinstance(next_cursor, str) or len(next_cursor) > MAX_CURSOR_CHARS
            ):
                raise ChannelError("POLL_STATE_INVALID", "Weixin response cursor is invalid")
            accepted = rejected = 0
            for message in batch.msgs:
                try:
                    result = await self.dispatch_authenticated_message(
                        account_id, message, expected_revision=revision,
                    )
                    accepted += int(not result.get("duplicate", False))
                except ChannelError as exc:
                    if exc.code == "CHANNEL_POLICY_DISABLED":
                        return {"paused": True, "processed": accepted + rejected}
                    rejected += 1
                    self._record_error(account_id, exc.code, revision)
            self.store.save_poll_state(
                account_id, {"cursor": next_cursor if next_cursor is not None else cursor},
                expected_revision=revision,
            )
            return {"processed": len(batch.msgs), "accepted": accepted, "rejected": rejected}

    async def _poll_account(self, account_id: str, revision: int) -> None:
        while True:
            try:
                account = self.store.get_account(account_id)
                if not account.enabled or account.revision != revision:
                    return
                outcome = await self.poll_once(account_id)
                # Bounded pause also prevents an immediate empty response from
                # turning into a hot loop or monopolizing the event loop.
                await asyncio.sleep(1 if outcome.get("busy") or outcome.get("paused") else 0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Weixin poll failed; account diagnostic is available")
                await asyncio.sleep(POLL_RETRY_SECONDS)

    async def _supervise(self) -> None:
        next_cleanup = 0.0
        while True:
            try:
                moment = asyncio.get_running_loop().time()
                if moment >= next_cleanup:
                    self.store.cleanup()
                    next_cleanup = moment + 60
                active = {account.id: account for account in self.store.active_accounts()}
                for account_id, (revision, task) in list(self._pollers.items()):
                    if (account_id not in active or active[account_id].revision != revision
                            or task.done()):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del self._pollers[account_id]
                for account_id, account in active.items():
                    if account_id not in self._pollers:
                        self._pollers[account_id] = (
                            account.revision,
                            asyncio.create_task(
                                self._poll_account(account_id, account.revision),
                                name=f"ophira:weixin:{account_id}",
                            ),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Weixin account discovery failed")
            await asyncio.sleep(1)

    async def start(self) -> None:
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise(), name="ophira:weixin")

    async def stop(self) -> None:
        tasks = [task for _, task in self._pollers.values()]
        if self._supervisor is not None:
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pollers.clear()
        self._supervisor = None
