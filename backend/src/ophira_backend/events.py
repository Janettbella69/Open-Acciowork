from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import AsyncIterator
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .constants import TERMINAL_RUN_STATUSES, EventType
from .models import EventActor, EventEnvelope
from .storage import FileLedger

SECRET_KEY_FRAGMENTS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "session_token",
    "token",
}
MAX_EVENT_STRING_LENGTH = 20_000
MAX_EVENT_DEPTH = 12

BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
NAMED_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"
)
ANTHROPIC_KEY_PATTERN = re.compile(r"\bsk-ant-[A-Za-z0-9_-]+\b")


def _redact_text(value: str) -> str:
    redacted = BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = NAMED_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )
    redacted = ANTHROPIC_KEY_PATTERN.sub("[REDACTED_API_KEY]", redacted)
    return redacted


def redact(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Convert arbitrary SDK values to bounded JSON-safe data and redact secrets."""
    # Numbers are never credentials; keys like "input_tokens" must keep their counters.
    if value is None or isinstance(value, bool | int | float):
        return value
    normalized_key = key.lower().replace("-", "_")
    if any(fragment in normalized_key for fragment in SECRET_KEY_FRAGMENTS):
        return "[REDACTED]"
    if depth >= MAX_EVENT_DEPTH:
        return "[TRUNCATED: maximum event depth]"
    if isinstance(value, str):
        value = _redact_text(value)
        if len(value) <= MAX_EVENT_STRING_LENGTH:
            return value
        return f"{value[:MAX_EVENT_STRING_LENGTH]}…[TRUNCATED]"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return redact(dataclasses.asdict(value), key=key, depth=depth + 1)
    if hasattr(value, "model_dump"):
        return redact(value.model_dump(mode="json"), key=key, depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(item_key): redact(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, depth=depth + 1) for item in value]
    return redact(str(value), key=key, depth=depth + 1)


class EventPublisher:
    def __init__(self, ledger: FileLedger) -> None:
        self.ledger = ledger
        self._conditions: dict[str, asyncio.Condition] = {}
        self._publish_locks: dict[str, asyncio.Lock] = {}

    def _condition(self, run_id: str) -> asyncio.Condition:
        return self._conditions.setdefault(run_id, asyncio.Condition())

    def _publish_lock(self, run_id: str) -> asyncio.Lock:
        return self._publish_locks.setdefault(run_id, asyncio.Lock())

    async def publish(
        self,
        run_id: str,
        event_type: EventType,
        data: dict[str, Any] | None = None,
        *,
        actor: EventActor | None = None,
        parent_id: str | None = None,
        tool_use_id: str | None = None,
    ) -> EventEnvelope:
        async with self._publish_lock(run_id):
            run = self.ledger.get_run(run_id)
            event = EventEnvelope(
                id="pending",
                sequence=1,
                type=event_type.value,
                run_id=run.id,
                project_id=run.project_id,
                trace_id=run.trace_id,
                sdk_session_id=run.sdk_session_id,
                provider_profile_id=run.provider_profile_id,
                actor=actor or EventActor(type="agent", name="ophira-main"),
                parent_id=parent_id,
                tool_use_id=tool_use_id,
                data=redact(data or {}),
            )
            stored = self.ledger.append_event(event)
        condition = self._condition(run_id)
        async with condition:
            condition.notify_all()
        return stored

    async def stream(
        self,
        run_id: str,
        after_sequence: int = 0,
        heartbeat_seconds: float = 15,
    ) -> AsyncIterator[EventEnvelope | None]:
        cursor = after_sequence
        condition = self._condition(run_id)
        while True:
            events = self.ledger.list_events(run_id, cursor)
            for event in events:
                cursor = event.sequence
                yield event
            run = self.ledger.get_run(run_id)
            summary = self.ledger.get_run_summary(run_id)
            if run.status in TERMINAL_RUN_STATUSES and cursor >= summary.last_sequence:
                return
            try:
                async with condition:
                    await asyncio.wait_for(condition.wait(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield None
