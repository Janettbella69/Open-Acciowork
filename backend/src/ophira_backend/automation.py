"""Proactive-service stores (PRD §4.16 / FR-15 / FR-26).

NotificationStore is the central inbox: heartbeat results with changes, schedule
failures, and standing-intent hits land here; "nothing happened" stays silent.
IntentStore holds user-authorized standing intents and enforces the anti-nag
budget (cooldown / max triggers / expiry) before any trigger reaches the inbox.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Notification, StandingIntent, StandingIntentCreate, utc_now


class NotificationNotFoundError(KeyError):
    pass


class IntentNotFoundError(KeyError):
    pass


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
        raise ValueError("invalid identifier")
    return value


class _FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, item_id: str) -> Path:
        return self.root / f"{_safe_id(item_id)}.json"

    def _write(self, item) -> None:
        path = self._path(item.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(item.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)


class NotificationStore(_FileStore):
    def add(
        self,
        *,
        title: str,
        body: str = "",
        severity: str = "info",
        project_id: str | None = None,
        run_id: str | None = None,
        schedule_id: str | None = None,
        intent_id: str | None = None,
    ) -> Notification:
        notification = Notification(
            title=title,
            body=body,
            severity=severity,
            project_id=project_id,
            run_id=run_id,
            schedule_id=schedule_id,
            intent_id=intent_id,
        )
        with self._lock:
            self._write(notification)
        return notification

    def get(self, notification_id: str) -> Notification:
        path = self._path(notification_id)
        if not path.exists():
            raise NotificationNotFoundError(notification_id)
        return Notification.model_validate_json(path.read_text("utf-8"))

    def list(self, *, unread_only: bool = False, limit: int = 200) -> list[Notification]:
        items: list[Notification] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(Notification.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item.created_at, reverse=True)
        if unread_only:
            items = [item for item in items if not item.read]
        return items[:limit]

    def mark_read(self, notification_id: str) -> Notification:
        with self._lock:
            updated = self.get(notification_id).model_copy(update={"read": True})
            self._write(updated)
        return updated

    def mark_all_read(self) -> int:
        count = 0
        with self._lock:
            for item in self.list(unread_only=True, limit=10_000):
                self._write(item.model_copy(update={"read": True}))
                count += 1
        return count


class IntentStore(_FileStore):
    def create(
        self, payload: StandingIntentCreate, *, now: datetime | None = None
    ) -> StandingIntent:
        moment = now or utc_now()
        intent = StandingIntent(
            project_id=payload.project_id,
            intent=payload.intent,
            cooldown_hours=payload.cooldown_hours,
            max_triggers=payload.max_triggers,
            expires_at=moment + timedelta(days=payload.expires_days),
            created_at=moment,
        )
        with self._lock:
            self._write(intent)
        return intent

    def get(self, intent_id: str) -> StandingIntent:
        path = self._path(intent_id)
        if not path.exists():
            raise IntentNotFoundError(intent_id)
        return StandingIntent.model_validate_json(path.read_text("utf-8"))

    def list(
        self,
        *,
        project_id: str | None = None,
        enabled_only: bool = False,
    ) -> list[StandingIntent]:
        items: list[StandingIntent] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(StandingIntent.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        if project_id:
            items = [item for item in items if item.project_id == project_id]
        if enabled_only:
            items = [item for item in items if item.enabled]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def set_enabled(self, intent_id: str, enabled: bool) -> StandingIntent:
        with self._lock:
            updated = self.get(intent_id).model_copy(update={"enabled": enabled})
            self._write(updated)
        return updated

    def try_trigger(
        self, intent_id: str, *, now: datetime | None = None
    ) -> tuple[str, StandingIntent]:
        """Apply the anti-nag budget in order: disabled → expired → exhausted →
        cooldown → triggered. Only "triggered" may reach the inbox."""
        moment = (now or utc_now()).astimezone(UTC)
        with self._lock:
            intent = self.get(intent_id)
            if not intent.enabled:
                return "disabled", intent
            if moment >= intent.expires_at:
                disabled = intent.model_copy(update={"enabled": False})
                self._write(disabled)
                return "expired", disabled
            if intent.triggered_count >= intent.max_triggers:
                return "exhausted", intent
            if intent.last_triggered_at is not None:
                elapsed = moment - intent.last_triggered_at
                if elapsed < timedelta(hours=intent.cooldown_hours):
                    return "cooldown", intent
            triggered = intent.model_copy(
                update={
                    "triggered_count": intent.triggered_count + 1,
                    "last_triggered_at": moment,
                }
            )
            self._write(triggered)
            return "triggered", triggered
