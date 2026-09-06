"""Private, bounded Web Push subscriptions and coalesced wake delivery state."""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import APIModel, new_id, utc_now
from .push_transport import PushSubscription


class PushWake(APIModel):
    id: str = Field(default_factory=lambda: new_id("wake"))
    anchors: list[str] = Field(max_length=200)
    state: Literal["pending", "attempting", "accepted", "failed", "cancelled"] = "pending"
    attempts: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    next_attempt_at: datetime = Field(default_factory=utc_now)
    reason: str | None = None


class PushRecord(APIModel):
    id: str = Field(pattern=r"^pair_[a-f0-9]{32}$")
    generation: str = Field(default_factory=lambda: new_id("push"))
    subscription: PushSubscription | None = Field(default=None, repr=False)
    public_key: str
    # Old inbox contents are not replayed when a user enables notifications.
    registered_at: datetime = Field(default_factory=utc_now)
    known_floor: datetime = Field(default_factory=utc_now)
    known: dict[str, datetime] = Field(default_factory=dict, max_length=1_000)
    wake: PushWake | None = None
    last_attempt_at: datetime | None = None
    retry_not_before: datetime | None = None
    last_accepted_at: datetime | None = None
    attempt_day: str = ""
    daily_attempts: int = 0
    disabled_reason: str | None = None


class PushStore:
    def __init__(self, root: Path):
        if root.is_symlink():
            raise ValueError("push store must not be a symlink")
        self.root = root.resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        # mkdir(mode=...) does not repair permissions on pre-existing/restored
        # directories; tighten the capability store before opening any records.
        self.root.chmod(0o700)
        self._lock = threading.RLock()

    def _path(self, device_id: str) -> Path:
        if not re.fullmatch(r"pair_[a-f0-9]{32}", device_id):
            raise ValueError("invalid device identifier")
        return self.root / f"{device_id}.json"

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            descriptor = os.open(
                self.root / ".transaction.lock",
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    @contextmanager
    def delivery_lease(self, device_id: str) -> Iterator[bool]:
        path = self._path(device_id).with_suffix(".lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(descriptor)

    def get(self, device_id: str) -> PushRecord | None:
        path = self._path(device_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError("push record is unavailable")
        return PushRecord.model_validate_json(path.read_text("utf-8"))

    def list_ids(self) -> list[str]:
        return [path.stem for path in self.root.glob("pair_*.json")
                if re.fullmatch(r"pair_[a-f0-9]{32}", path.stem)]

    def _write(self, record: PushRecord) -> None:
        # Validate copies too; no growing unbounded queue/history on disk.
        record = PushRecord.model_validate(record.model_dump())
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.root, prefix=".push-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, self._path(record.id))
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def subscribe(
        self, device_id: str, subscription: PushSubscription, public_key: str,
    ) -> PushRecord:
        with self._transaction():
            for other_id in self.list_ids():
                other = self.get(other_id)
                if (other and other.subscription and other.id != device_id
                        and other.subscription.destination_identity
                        == subscription.destination_identity):
                    raise ValueError("subscription is already bound to another device")
            current = self.get(device_id)
            if (current and current.subscription == subscription
                    and current.public_key == public_key
                    and current.disabled_reason is None):
                return current  # Retries do not reset the cursor or anti-nag budget.
            now = utc_now()
            updated = PushRecord(
                id=device_id, subscription=subscription, public_key=public_key,
                registered_at=now, known_floor=now,
                last_attempt_at=current.last_attempt_at if current else None,
                retry_not_before=current.retry_not_before if current else None,
                attempt_day=current.attempt_day if current else "",
                daily_attempts=current.daily_attempts if current else 0,
            )
            self._write(updated)
            return updated

    def save(self, record: PushRecord) -> bool:
        with self._transaction():
            current = self.get(record.id)
            if current is None or current.generation != record.generation:
                return False  # Old HTTP attempt cannot restore an unsubscribed/replaced capability.
            self._write(record)
            return True

    def unsubscribe(
        self, device_id: str, reason: str = "unsubscribed", *, generation: str | None = None,
    ) -> None:
        with self._transaction():
            current = self.get(device_id)
            if current is None or (generation is not None and current.generation != generation):
                return
            # Scrub capability endpoints and encryption keys; retain only bounded delivery metadata.
            current.subscription = None
            current.generation = new_id("push")
            current.disabled_reason = reason
            if current.wake and current.wake.state in {"pending", "attempting"}:
                current.wake.state = "cancelled"
                current.wake.reason = reason
            self._write(current)
