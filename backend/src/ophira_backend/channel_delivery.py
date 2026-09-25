"""Durable outbound channel replies for verified IM-originated runs (FR-24/38).

The delivery ledger closes the crash gap between a terminal Agent run and the
platform acknowledgement. Credentials and tenant tokens live only in adapters;
neither is persisted in this ledger or exposed to the Agent/event stream.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .constants import EventType, RunStatus
from .events import redact
from .models import RunRecord, utc_now
from .project_lifecycle import (
    ProjectLifecycle,
    ProjectLifecycleBusyError,
    ProjectUnavailableError,
)
from .storage import FileLedger, LedgerNotFoundError

logger = logging.getLogger(__name__)

MAX_FEISHU_RESPONSE_BYTES = 1_000_000
MAX_CHANNEL_REPLY_CHARS = 20_000
FEISHU_IDEMPOTENCY_WINDOW = timedelta(hours=1)
MAX_RECOVERY_BATCH = 50


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    ATTEMPTING = "attempting"
    DELIVERED = "delivered"
    FAILED = "failed"


class DeliveryLedgerError(RuntimeError):
    pass


class ChannelDeliveryError(RuntimeError):
    """Safe, classified adapter failure; never carries credentials or response bodies."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class FeishuReplyTarget(BaseModel):
    """Reply address captured only after the inbound Feishu request is verified."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["feishu"] = "feishu"
    event_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    chat_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    chat_type: Literal["p2p", "group", "topic", "unknown"] = "unknown"
    thread_id: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]{1,200}$",
    )
    reply_in_thread: bool = False

    @model_validator(mode="after")
    def thread_scope_is_coherent(self) -> FeishuReplyTarget:
        if self.reply_in_thread != (self.thread_id is not None):
            raise ValueError("reply_in_thread must match the captured thread scope")
        return self


class DingTalkReplyTarget(BaseModel):
    """Reply scope captured from an authenticated DingTalk Stream callback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["dingtalk"] = "dingtalk"
    event_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=256)
    chat_type: Literal["group", "p2p"]
    sender_staff_id: str = Field(min_length=1, max_length=256)
    robot_code: str = Field(min_length=1, max_length=160)


class WeComReplyTarget(BaseModel):
    """Recipient captured from an authenticated WeCom intelligent bot frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["wecom"] = "wecom"
    event_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=256)
    chat_type: Literal["group", "single"]
    sender_user_id: str = Field(min_length=1, max_length=256)
    bot_id: str = Field(min_length=1, max_length=256)


ChannelReplyTarget = FeishuReplyTarget | DingTalkReplyTarget | WeComReplyTarget


class DeliveryTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DeliveryStatus
    at: datetime
    attempt: int = Field(ge=0)
    code: str | None = Field(default=None, max_length=120)


class ChannelDeliveryRecord(BaseModel):
    """One immutable-target, mutable-state delivery obligation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: str = Field(pattern=r"^delivery_[a-f0-9]{40}$")
    idempotency_key: str = Field(min_length=1, max_length=50)
    channel: Literal["feishu", "dingtalk", "wecom"] = "feishu"
    run_id: str = Field(min_length=1, max_length=160)
    project_id: str = Field(min_length=1, max_length=160)
    target: ChannelReplyTarget = Field(discriminator="channel")
    status: DeliveryStatus = DeliveryStatus.PENDING
    content: str | None = Field(default=None, max_length=MAX_CHANNEL_REPLY_CHARS)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1, le=20)
    retryable: bool = True
    next_attempt_at: datetime | None = None
    first_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    external_message_id: str | None = Field(default=None, max_length=200)
    external_chat_id: str | None = Field(default=None, max_length=256)
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_detail: str | None = Field(default=None, max_length=500)
    history: list[DeliveryTransition] = Field(min_length=1, max_length=100)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent_state(self) -> ChannelDeliveryRecord:
        if self.channel != self.target.channel:
            raise ValueError("delivery channel does not match its target")
        timestamps = [self.created_at, self.updated_at]
        timestamps.extend(
            value
            for value in (
                self.next_attempt_at,
                self.first_attempt_at,
                self.last_attempt_at,
                self.delivered_at,
            )
            if value is not None
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("delivery timestamps must be timezone-aware")
        if self.content is None and self.content_hash is not None:
            raise ValueError("content hash cannot exist without content")
        if self.content is not None and _content_hash(self.content) != self.content_hash:
            raise ValueError("delivery content hash mismatch")
        if self.status == DeliveryStatus.DELIVERED:
            if self.delivered_at is None or not self.external_message_id:
                raise ValueError("delivered records require acknowledgement metadata")
            if self.retryable or self.next_attempt_at is not None:
                raise ValueError("delivered records cannot remain retryable")
        if self.status == DeliveryStatus.ATTEMPTING and self.last_attempt_at is None:
            raise ValueError("attempting records require an attempt timestamp")
        if self.attempt_count > self.max_attempts:
            raise ValueError("delivery attempt count exceeds its bound")
        if self.history[-1].status != self.status:
            raise ValueError("delivery history must end at the current status")
        return self


class FeishuReplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1, max_length=200)
    chat_id: str | None = Field(default=None, max_length=256)


ChannelReplyResult = FeishuReplyResult


class ChannelReplyAdapter(Protocol):
    @property
    def configured(self) -> bool: ...

    async def reply(
        self,
        target: ChannelReplyTarget,
        text: str,
        *,
        idempotency_key: str,
    ) -> FeishuReplyResult: ...


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_detail(value: str) -> str:
    return str(redact(value)).replace("\n", " ")[:500]


def _delivery_identity(run_id: str, target: ChannelReplyTarget) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{target.channel}-reply-v1\0{run_id}\0{target.message_id}".encode()
    ).hexdigest()
    return f"delivery_{digest[:40]}", digest[:48]


class ChannelDeliveryStore:
    """Atomic JSON snapshots for reply obligations, keyed deterministically by Run."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._lock_state = threading.local()

    @contextmanager
    def _transaction(self):
        with self._lock:
            if getattr(self._lock_state, "depth", 0):
                self._lock_state.depth += 1
                try:
                    yield
                finally:
                    self._lock_state.depth -= 1
                return
            descriptor = os.open(
                self.root / ".deliveries.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._lock_state.depth = 1
                try:
                    yield
                finally:
                    self._lock_state.depth = 0
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def attempt_lease(self, delivery_id: str):
        """Keep recovery off an active network attempt in another worker."""

        descriptor = os.open(
            self.root / f"{self._safe_id(delivery_id)}.attempt.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"delivery_[a-f0-9]{40}", value):
            raise ValueError("invalid delivery identifier")
        return value

    def _path(self, delivery_id: str) -> Path:
        return self.root / f"{self._safe_id(delivery_id)}.json"

    def _write(self, record: ChannelDeliveryRecord) -> None:
        with ProjectLifecycle(self.root.parent).lease(record.project_id):
            ProjectLifecycle(self.root.parent).assert_run_available(
                record.project_id, record.run_id,
            )
            path = self._path(record.id)
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
            try:
                descriptor = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(record.model_dump_json(indent=2) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass

    def bind_run(
        self,
        run: RunRecord,
        target: ChannelReplyTarget,
        *,
        max_attempts: int,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        moment = (now or utc_now()).astimezone(UTC)
        delivery_id, idempotency_key = _delivery_identity(run.id, target)
        with self._transaction():
            path = self._path(delivery_id)
            if path.exists():
                existing = self.get(delivery_id)
                if (
                    existing.run_id != run.id
                    or existing.project_id != run.project_id
                    or existing.target != target
                ):
                    raise DeliveryLedgerError("delivery identity is bound to another scope")
                return existing
            record = ChannelDeliveryRecord(
                id=delivery_id,
                idempotency_key=idempotency_key,
                channel=target.channel,
                run_id=run.id,
                project_id=run.project_id,
                target=target,
                max_attempts=max_attempts,
                next_attempt_at=moment,
                history=[
                    DeliveryTransition(
                        status=DeliveryStatus.PENDING,
                        at=moment,
                        attempt=0,
                    )
                ],
                created_at=moment,
                updated_at=moment,
            )
            self._write(record)
            return record

    def get(self, delivery_id: str) -> ChannelDeliveryRecord:
        path = self._path(delivery_id)
        if not path.is_file() or path.is_symlink():
            raise DeliveryLedgerError("delivery record not found")
        try:
            record = ChannelDeliveryRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.id != delivery_id:
                raise ValueError("delivery filename does not match its record")
            return record
        except (OSError, ValueError) as exc:
            raise DeliveryLedgerError("delivery record failed validation") from exc

    def get_for_run(self, run_id: str) -> ChannelDeliveryRecord | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("invalid run identifier")
        for record in self.list():
            if record.run_id == run_id:
                return record
        return None

    def list(self) -> list[ChannelDeliveryRecord]:
        records: list[ChannelDeliveryRecord] = []
        for path in self.root.glob("delivery_*.json"):
            try:
                if path.is_symlink():
                    raise ValueError("symlink delivery record")
                record = ChannelDeliveryRecord.model_validate_json(path.read_text(encoding="utf-8"))
                if path.name != f"{record.id}.json":
                    raise ValueError("delivery filename does not match its record")
                if not ProjectLifecycle(self.root.parent).has_deletion(record.project_id):
                    records.append(record)
            except (OSError, ValueError) as exc:
                raise DeliveryLedgerError(
                    f"delivery record {path.name!r} failed validation"
                ) from exc
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def attach_result(
        self,
        delivery_id: str,
        content: str,
        *,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        normalized = _bounded_reply(content)
        moment = (now or utc_now()).astimezone(UTC)
        with self._transaction():
            record = self.get(delivery_id)
            if record.content is not None and record.content != normalized:
                raise DeliveryLedgerError("terminal run result changed after delivery was queued")
            if record.content is not None:
                return record
            updated = record.model_copy(
                update={
                    "content": normalized,
                    "content_hash": _content_hash(normalized),
                    "next_attempt_at": moment,
                    "updated_at": moment,
                }
            )
            self._write(updated)
            return updated

    def claim_due(
        self,
        delivery_id: str,
        *,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord | None:
        moment = (now or utc_now()).astimezone(UTC)
        with self._transaction():
            record = self.get(delivery_id)
            eligible = record.status == DeliveryStatus.PENDING or (
                record.status == DeliveryStatus.FAILED and record.retryable
            )
            if (
                not eligible
                or record.content is None
                or (record.next_attempt_at is not None and record.next_attempt_at > moment)
            ):
                return None
            if (
                record.channel == "feishu"
                and record.first_attempt_at is not None
                and moment - record.first_attempt_at >= FEISHU_IDEMPOTENCY_WINDOW
            ):
                return self._terminal_idempotency_expired(record, moment)
            if record.attempt_count >= record.max_attempts:
                return self._terminal_exhausted(record, moment)
            attempt = record.attempt_count + 1
            updated = record.model_copy(
                update={
                    "status": DeliveryStatus.ATTEMPTING,
                    "attempt_count": attempt,
                    "first_attempt_at": record.first_attempt_at or moment,
                    "last_attempt_at": moment,
                    "next_attempt_at": None,
                    "last_error_code": None,
                    "last_error_detail": None,
                    "history": [
                        *record.history,
                        DeliveryTransition(
                            status=DeliveryStatus.ATTEMPTING,
                            at=moment,
                            attempt=attempt,
                        ),
                    ],
                    "updated_at": moment,
                }
            )
            self._write(updated)
            return updated

    def mark_delivered(
        self,
        delivery_id: str,
        result: FeishuReplyResult,
        *,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        moment = (now or utc_now()).astimezone(UTC)
        with self._transaction():
            record = self.get(delivery_id)
            if record.status == DeliveryStatus.DELIVERED:
                return record
            if record.status != DeliveryStatus.ATTEMPTING:
                raise DeliveryLedgerError("only an attempting delivery can be acknowledged")
            updated = record.model_copy(
                update={
                    "status": DeliveryStatus.DELIVERED,
                    "retryable": False,
                    "next_attempt_at": None,
                    "delivered_at": moment,
                    "external_message_id": result.message_id,
                    "external_chat_id": result.chat_id,
                    "history": [
                        *record.history,
                        DeliveryTransition(
                            status=DeliveryStatus.DELIVERED,
                            at=moment,
                            attempt=record.attempt_count,
                        ),
                    ],
                    "updated_at": moment,
                }
            )
            self._write(updated)
            return updated

    def mark_failed(
        self,
        delivery_id: str,
        *,
        code: str,
        detail: str,
        retryable: bool,
        retry_delay_seconds: float | None,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        moment = (now or utc_now()).astimezone(UTC)
        with self._transaction():
            record = self.get(delivery_id)
            if record.status != DeliveryStatus.ATTEMPTING:
                raise DeliveryLedgerError("only an attempting delivery can fail")
            can_retry = retryable and record.attempt_count < record.max_attempts
            next_attempt = (
                moment + timedelta(seconds=max(0.0, retry_delay_seconds or 0.0))
                if can_retry
                else None
            )
            final_code = code if can_retry or not retryable else "RETRY_EXHAUSTED"
            updated = record.model_copy(
                update={
                    "status": DeliveryStatus.FAILED,
                    "retryable": can_retry,
                    "next_attempt_at": next_attempt,
                    "last_error_code": final_code,
                    "last_error_detail": _safe_detail(detail),
                    "history": [
                        *record.history,
                        DeliveryTransition(
                            status=DeliveryStatus.FAILED,
                            at=moment,
                            attempt=record.attempt_count,
                            code=final_code,
                        ),
                    ],
                    "updated_at": moment,
                }
            )
            self._write(updated)
            return updated

    def recover_attempting(self, *, now: datetime | None = None) -> list[ChannelDeliveryRecord]:
        """Replay Feishu within its UUID window; quarantine other ambiguous sends."""

        moment = (now or utc_now()).astimezone(UTC)
        recovered: list[ChannelDeliveryRecord] = []
        for snapshot in self.list():
            if snapshot.status != DeliveryStatus.ATTEMPTING:
                continue
            with self.attempt_lease(snapshot.id) as available:
                if not available:
                    continue
                record = self.get(snapshot.id)
                if record.status != DeliveryStatus.ATTEMPTING:
                    continue
                if record.channel in {"dingtalk", "wecom"}:
                    recovered.append(
                        self.mark_failed(
                            record.id,
                            code="UNCERTAIN_OUTCOME",
                            detail=(
                                "Channel send was interrupted before acknowledgement; "
                                "inspect the conversation before a new manual send."
                            ),
                            retryable=False,
                            retry_delay_seconds=None,
                            now=moment,
                        )
                    )
                    continue
                if (
                    record.first_attempt_at is not None
                    and moment - record.first_attempt_at >= FEISHU_IDEMPOTENCY_WINDOW
                ):
                    recovered.append(
                        self.mark_failed(
                            record.id,
                            code="IDEMPOTENCY_WINDOW_EXPIRED",
                            detail=(
                                "The prior attempt has an ambiguous outcome and Feishu's "
                                "one-hour deduplication window expired; automatic replay stopped."
                            ),
                            retryable=False,
                            retry_delay_seconds=None,
                            now=moment,
                        )
                    )
                    continue
                recovered.append(
                    self.mark_failed(
                        record.id,
                        code="PROCESS_INTERRUPTED",
                        detail="A previous attempt was interrupted before acknowledgement.",
                        retryable=True,
                        retry_delay_seconds=0,
                        now=moment,
                    )
                )
        return recovered

    def due(self, *, now: datetime | None = None) -> list[ChannelDeliveryRecord]:
        moment = (now or utc_now()).astimezone(UTC)
        return [
            record
            for record in self.list()
            if record.content is not None
            and (
                record.status == DeliveryStatus.PENDING
                or (record.status == DeliveryStatus.FAILED and record.retryable)
            )
            and (record.next_attempt_at is None or record.next_attempt_at <= moment)
        ]

    def _terminal_exhausted(
        self,
        record: ChannelDeliveryRecord,
        moment: datetime,
    ) -> ChannelDeliveryRecord:
        updated = record.model_copy(
            update={
                "status": DeliveryStatus.FAILED,
                "retryable": False,
                "next_attempt_at": None,
                "last_error_code": "RETRY_EXHAUSTED",
                "last_error_detail": "The configured delivery attempt limit was reached.",
                "history": [
                    *record.history,
                    DeliveryTransition(
                        status=DeliveryStatus.FAILED,
                        at=moment,
                        attempt=record.attempt_count,
                        code="RETRY_EXHAUSTED",
                    ),
                ],
                "updated_at": moment,
            }
        )
        self._write(updated)
        return updated

    def _terminal_idempotency_expired(
        self,
        record: ChannelDeliveryRecord,
        moment: datetime,
    ) -> ChannelDeliveryRecord:
        updated = record.model_copy(
            update={
                "status": DeliveryStatus.FAILED,
                "retryable": False,
                "next_attempt_at": None,
                "last_error_code": "IDEMPOTENCY_WINDOW_EXPIRED",
                "last_error_detail": (
                    "Feishu's one-hour deduplication window expired before replay; "
                    "inspect the conversation before a new manual send."
                ),
                "history": [
                    *record.history,
                    DeliveryTransition(
                        status=DeliveryStatus.FAILED,
                        at=moment,
                        attempt=record.attempt_count,
                        code="IDEMPOTENCY_WINDOW_EXPIRED",
                    ),
                ],
                "updated_at": moment,
            }
        )
        self._write(updated)
        return updated


def _retry_after_seconds(response: httpx.Response, maximum: float) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(maximum, max(0.0, float(raw)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return min(maximum, max(0.0, (parsed - utc_now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


class FeishuReplyAdapter:
    """Official Feishu tenant-token + reply-message HTTP adapter."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_id = settings.feishu_app_id or ""
        self._app_secret = (
            settings.feishu_app_secret.get_secret_value()
            if settings.feishu_app_secret is not None
            else ""
        )
        self._base_url = validate_feishu_api_base_url(settings.feishu_api_base_url)
        self._timeout = settings.feishu_delivery_timeout_seconds
        self._retry_max_seconds = settings.feishu_delivery_retry_max_seconds
        self._transport = transport
        self._token: str | None = None
        self._token_expires_monotonic = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._app_secret)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, headers=headers, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ChannelDeliveryError(
                "NETWORK_ERROR",
                f"Feishu request failed before acknowledgement ({type(exc).__name__}).",
                retryable=True,
            ) from exc
        if len(response.content) > MAX_FEISHU_RESPONSE_BYTES:
            raise ChannelDeliveryError(
                "RESPONSE_TOO_LARGE",
                "Feishu returned an oversized response.",
                retryable=False,
            )
        if response.status_code == 429:
            raise ChannelDeliveryError(
                "HTTP_429",
                "Feishu rate-limited the request.",
                retryable=True,
                retry_after_seconds=_retry_after_seconds(
                    response,
                    self._retry_max_seconds,
                ),
            )
        if response.status_code >= 500:
            raise ChannelDeliveryError(
                f"HTTP_{response.status_code}",
                "Feishu returned a server error.",
                retryable=True,
            )
        if 400 <= response.status_code < 500:
            raise ChannelDeliveryError(
                f"HTTP_{response.status_code}",
                "Feishu rejected the request; automatic retry is disabled.",
                retryable=False,
            )
        if not 200 <= response.status_code < 300:
            raise ChannelDeliveryError(
                f"HTTP_{response.status_code}",
                "Feishu returned an unexpected redirect or status.",
                retryable=False,
            )
        return response

    @staticmethod
    def _json_object(response: httpx.Response, *, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChannelDeliveryError(
                "MALFORMED_RESPONSE",
                f"Feishu {operation} response was not JSON.",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise ChannelDeliveryError(
                "MALFORMED_RESPONSE",
                f"Feishu {operation} response was not an object.",
                retryable=False,
            )
        return payload

    async def _tenant_access_token(self) -> str:
        if not self.configured:
            raise ChannelDeliveryError(
                "CHANNEL_NOT_CONFIGURED",
                "Feishu app credentials are not configured.",
                retryable=False,
            )
        current = time.monotonic()
        if self._token and current < self._token_expires_monotonic:
            return self._token
        async with self._token_lock:
            current = time.monotonic()
            if self._token and current < self._token_expires_monotonic:
                return self._token
            response = await self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json_body={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            payload = self._json_object(response, operation="tenant-token")
            if payload.get("code") != 0:
                code = payload.get("code")
                raise ChannelDeliveryError(
                    f"FEISHU_TOKEN_CODE_{code}",
                    "Feishu did not issue a tenant access token.",
                    retryable=False,
                )
            token = payload.get("tenant_access_token")
            expires = payload.get("expire")
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expires, int)
                or isinstance(expires, bool)
                or expires <= 0
            ):
                raise ChannelDeliveryError(
                    "MALFORMED_TOKEN_RESPONSE",
                    "Feishu tenant-token response omitted required fields.",
                    retryable=False,
                )
            # Keep a margin without making short-lived test tokens immediately stale.
            margin = min(300, max(1, expires // 10))
            self._token = token
            self._token_expires_monotonic = current + max(1, expires - margin)
            return token

    async def reply(
        self,
        target: FeishuReplyTarget,
        text: str,
        *,
        idempotency_key: str,
    ) -> FeishuReplyResult:
        if len(idempotency_key) > 50:
            raise ChannelDeliveryError(
                "INVALID_IDEMPOTENCY_KEY",
                "Feishu idempotency keys may not exceed 50 characters.",
                retryable=False,
            )
        token = await self._tenant_access_token()
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{target.message_id}/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json_body={
                "content": json.dumps(
                    {"text": _bounded_reply(text)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "msg_type": "text",
                "reply_in_thread": target.reply_in_thread,
                "uuid": idempotency_key,
            },
        )
        payload = self._json_object(response, operation="reply")
        if payload.get("code") != 0:
            code = payload.get("code")
            raise ChannelDeliveryError(
                f"FEISHU_REPLY_CODE_{code}",
                "Feishu reply API returned a non-success code.",
                retryable=False,
            )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("message_id"), str):
            raise ChannelDeliveryError(
                "MALFORMED_REPLY_RESPONSE",
                "Feishu reply response omitted the delivered message ID.",
                retryable=False,
            )
        response_chat_id = data.get("chat_id")
        if response_chat_id is not None and response_chat_id != target.chat_id:
            raise ChannelDeliveryError(
                "CHAT_SCOPE_MISMATCH",
                "Feishu acknowledgement did not match the verified inbound chat.",
                retryable=False,
            )
        return FeishuReplyResult(
            message_id=data["message_id"],
            chat_id=response_chat_id if isinstance(response_chat_id, str) else None,
        )


def _bounded_reply(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        normalized = "运行已结束，但没有可显示的文本结果。"
    if len(normalized) <= MAX_CHANNEL_REPLY_CHARS:
        return normalized
    suffix = "\n\n…[结果过长，已截断；完整内容请在 OPHIRA 工作台查看]"
    return normalized[: MAX_CHANNEL_REPLY_CHARS - len(suffix)] + suffix


class ChannelDeliveryService:
    """Queues terminal Run results and dispatches durable channel obligations."""

    def __init__(
        self,
        ledger: FileLedger,
        store: ChannelDeliveryStore,
        adapter: ChannelReplyAdapter,
        *,
        dingtalk_adapter: ChannelReplyAdapter | None = None,
        wecom_adapter: ChannelReplyAdapter | None = None,
        enabled: Callable[[], bool] | None = None,
        max_attempts: int = 4,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 60.0,
        worker_interval_seconds: float = 5.0,
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.adapter = adapter
        self.dingtalk_adapter = dingtalk_adapter
        self.wecom_adapter = wecom_adapter
        self.enabled = enabled or (lambda: True)
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.worker_interval_seconds = worker_interval_seconds
        self._worker: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return self.adapter.configured

    def _adapter_for(self, channel: Literal["feishu", "dingtalk", "wecom"]):
        return {
            "feishu": self.adapter,
            "dingtalk": self.dingtalk_adapter,
            "wecom": self.wecom_adapter,
        }[channel]

    def configured_for(self, channel: Literal["feishu", "dingtalk", "wecom"]) -> bool:
        adapter = self._adapter_for(channel)
        return adapter is not None and adapter.configured

    def bind_run(self, run: RunRecord, target: ChannelReplyTarget) -> ChannelDeliveryRecord:
        return self.store.bind_run(run, target, max_attempts=self.max_attempts)

    async def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        # Reset crash-interrupted records synchronously, then perform network I/O
        # in the worker so a slow Feishu endpoint cannot block application startup.
        self.store.recover_attempting()
        self._worker = asyncio.create_task(self._loop(), name="ophira:channel-delivery")

    async def stop(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception as exc:  # noqa: BLE001 - keep the recovery worker alive
                logger.error("channel delivery reconciliation failed: %s", _safe_detail(str(exc)))
            await asyncio.sleep(self.worker_interval_seconds)

    async def finalize_run(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord | None:
        record = self.store.get_for_run(run_id)
        if record is None:
            return None
        run = self.ledger.get_run(run_id)
        content = self._terminal_result(run)
        if content is None:
            return record
        record = self.store.attach_result(record.id, content, now=now)
        return await self._attempt(record.id, now=now)

    async def reconcile(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ChannelDeliveryRecord]:
        moment = (now or utc_now()).astimezone(UTC)
        self.store.recover_attempting(now=moment)
        results: list[ChannelDeliveryRecord] = []
        # Recover the crash window after the Run reached terminal status but before
        # its final text was attached to the already-durable delivery obligation.
        for record in self.store.list():
            if record.content is not None or record.status == DeliveryStatus.DELIVERED:
                continue
            try:
                run = self.ledger.get_run(record.run_id)
            except LedgerNotFoundError:
                continue
            content = self._terminal_result(run)
            if content is not None:
                self.store.attach_result(record.id, content, now=moment)
        for record in self.store.due(now=moment)[:MAX_RECOVERY_BATCH]:
            results.append(await self._attempt(record.id, now=moment))
        return results

    def _terminal_result(self, run: RunRecord) -> str | None:
        if run.status == RunStatus.COMPLETED:
            text = ""
            for event in self.ledger.list_events(run.id):
                if event.type == EventType.RUN_RESULT.value:
                    text = str(event.data.get("result") or text)
                elif event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
                    text = str(event.data.get("text") or text)
            return text.strip() or (
                f"运行 {run.id} 已完成，但没有可显示的文本结果。请在 OPHIRA 工作台查看详情。"
            )
        if run.status == RunStatus.FAILED:
            code = getattr(run.error_code, "value", run.error_code) or "RUN_FAILED"
            detail = run.error or "运行失败，未返回更多安全错误信息。"
            return f"任务执行失败（{code}）：{detail}\n运行 ID：{run.id}"
        return None

    async def _attempt(
        self,
        delivery_id: str,
        *,
        now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        record = self.store.get(delivery_id)
        try:
            with self.ledger.lifecycle.lease(record.project_id):
                with self.store.attempt_lease(delivery_id) as available:
                    if not available:
                        return self.store.get(delivery_id)
                    return await self._attempt_owned(delivery_id, now=now)
        except (ProjectUnavailableError, ProjectLifecycleBusyError):
            return record

    async def _attempt_owned(
        self, delivery_id: str, *, now: datetime | None = None,
    ) -> ChannelDeliveryRecord:
        moment = (now or utc_now()).astimezone(UTC)
        current = self.store.get(delivery_id)
        if (
            current.channel == "wecom"
            and self.enabled()
            and self.wecom_adapter is not None
            and self.wecom_adapter.configured
            and not getattr(self.wecom_adapter, "ready", True)
        ):
            return current
        claimed = self.store.claim_due(delivery_id, now=moment)
        if claimed is None:
            return self.store.get(delivery_id)
        if claimed.status != DeliveryStatus.ATTEMPTING:
            return claimed
        if not self.enabled():
            return self.store.mark_failed(
                delivery_id,
                code="CHANNEL_POLICY_DISABLED",
                detail="Organization policy disabled channel delivery.",
                retryable=False,
                retry_delay_seconds=None,
                now=moment,
            )
        adapter = self._adapter_for(claimed.channel)
        if adapter is None or not adapter.configured:
            return self.store.mark_failed(
                delivery_id,
                code="CHANNEL_NOT_CONFIGURED",
                detail=f"{claimed.channel} app credentials are not configured.",
                retryable=False,
                retry_delay_seconds=None,
                now=moment,
            )
        try:
            result = await adapter.reply(
                claimed.target,
                claimed.content or "",
                idempotency_key=claimed.idempotency_key,
            )
        except ChannelDeliveryError as exc:
            fallback = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, claimed.attempt_count - 1)),
            )
            delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else fallback
            return self.store.mark_failed(
                delivery_id,
                code=exc.code,
                detail=str(exc),
                retryable=exc.retryable,
                retry_delay_seconds=delay,
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary must remain durable
            return self.store.mark_failed(
                delivery_id,
                code="ADAPTER_EXCEPTION",
                detail=f"Channel adapter raised {type(exc).__name__}.",
                retryable=claimed.channel == "feishu",
                retry_delay_seconds=min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** max(0, claimed.attempt_count - 1)),
                ),
                now=moment,
            )
        return self.store.mark_delivered(delivery_id, result, now=moment)


def validate_feishu_api_base_url(value: str) -> str:
    """Keep app credentials pinned to the two official OpenAPI origins."""

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("invalid Feishu API base URL") from exc
    if (
        parts.scheme != "https"
        or parts.hostname not in {"open.feishu.cn", "open.larksuite.com"}
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("Feishu API base URL must use an official HTTPS OpenAPI origin")
    return f"https://{parts.hostname}"
