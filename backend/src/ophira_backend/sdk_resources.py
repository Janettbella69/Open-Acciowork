"""Bound SDK-observed output and active time without changing its tool loop.

The quota is normalized SDK message JSON plus stderr UTF-8, not wire traffic,
workspace size, process RSS, or private reasoning storage. Reservations are
durable *before* a message is admitted; a crash can waste a reservation but
cannot replenish the output allowance. Only numeric usage is persisted here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from time import monotonic
from typing import Any

from .config import Settings
from .models import SdkResourceUsage, utc_now
from .storage import FileLedger

RESERVATION_BYTES = 65_536


class SdkResourceLimitError(RuntimeError):
    pass


def _message_object(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # No deep copy of large tool payloads or thinking blocks.
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    raise TypeError("Unsupported SDK message value")


class SdkExecutionBudget:
    def __init__(self, ledger: FileLedger, run_id: str, settings: Settings) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.exceeded = asyncio.Event()
        self.error: SdkResourceLimitError | None = None
        self._available = 0
        self._closed = False
        self._started = monotonic()
        with ledger._lock_for(run_id):
            current = ledger.get_run(run_id).sdk_resource_usage
            usage = current or SdkResourceUsage(
                active_limit_seconds=settings.sdk_active_timeout_seconds,
                output_limit_bytes=settings.sdk_output_limit_bytes,
                message_buffer_bytes=settings.sdk_message_buffer_bytes,
            )
            now = utc_now()
            if usage.active_started_at is not None:
                # A prior owner disappeared without closing. Charge the elapsed
                # interval conservatively rather than silently resetting it.
                usage.active_seconds_used += max(
                    0, (now - usage.active_started_at).total_seconds(),
                )
            self._remaining_seconds = max(
                0, usage.active_limit_seconds - usage.active_seconds_used,
            )
            usage.active_started_at = now
            ledger.update_run(run_id, sdk_resource_usage=usage)
        self.message_buffer_bytes = usage.message_buffer_bytes
        if self.remaining_seconds <= 0:
            self._fail("SDK active execution time limit was exhausted; saved work is preserved.")
        elif usage.output_bytes_reserved >= usage.output_limit_bytes:
            self._fail("SDK output allowance was exhausted; saved work is preserved.")
        if self.error is not None:
            self.exceeded.set()

    @property
    def remaining_seconds(self) -> float:
        return max(0, self._remaining_seconds - max(0, monotonic() - self._started))

    def _fail(self, message: str) -> SdkResourceLimitError:
        if self.error is None:
            self.error = SdkResourceLimitError(message)
        return self.error

    def consume(self, size: int) -> None:
        if self.error is not None:
            raise self.error
        if self._closed:
            raise RuntimeError("SDK output arrived after its execution budget closed")
        if size < 0:
            raise ValueError("Output size must be nonnegative")
        if size > self._available:
            needed = size - self._available
            with self.ledger._lock_for(self.run_id):
                usage = self.ledger.get_run(self.run_id).sdk_resource_usage
                assert usage is not None
                remaining = usage.output_limit_bytes - usage.output_bytes_reserved
                reserve = min(remaining, max(RESERVATION_BYTES, needed))
                usage.output_bytes_reserved += reserve
                self.ledger.update_run(self.run_id, sdk_resource_usage=usage)
            if needed > reserve:
                raise self._fail(
                    "SDK output allowance exceeded; the session was stopped and saved work "
                    "is preserved. Review output size before starting another Run."
                )
            self._available += reserve
        self._available -= size

    def message(self, message: Any) -> None:
        # Iteration avoids building another full serialization. Partial and final
        # SDK messages both count, including child messages, without saving any
        # private thinking content. Quota checks happen before event mapping.
        encoder = json.JSONEncoder(
            ensure_ascii=False, separators=(",", ":"), default=_message_object,
        )
        for chunk in encoder.iterencode(message):
            self.consume(len(chunk.encode("utf-8")))
        self.consume(1)  # message boundary

    def stderr(self, line: str) -> None:
        # The SDK isolates exceptions from stderr callbacks. Signal the owning
        # task as well, so stderr-only flooding still terminates the session.
        if self._closed or self.error is not None:
            return
        try:
            self.consume(len(line.encode("utf-8")) + 1)
        except SdkResourceLimitError:
            pass
        except Exception:
            self._fail("SDK output accounting failed; the session must stop to preserve its limit.")
        if self.error is not None:
            self.exceeded.set()

    def close(self) -> None:
        if self._closed:
            return
        elapsed = max(0, monotonic() - self._started)
        with self.ledger._lock_for(self.run_id):
            usage = self.ledger.get_run(self.run_id).sdk_resource_usage
            assert usage is not None
            usage.active_seconds_used += elapsed
            usage.active_started_at = None
            self.ledger.update_run(self.run_id, sdk_resource_usage=usage)
        self._closed = True
