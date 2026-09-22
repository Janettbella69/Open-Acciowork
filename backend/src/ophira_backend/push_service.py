"""Device-scoped, coalesced background wakes. No business content crosses push."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from time import monotonic

from .auth import ProjectAccessStore, ProjectPermission
from .automation import NotificationStore
from .companion import CompanionError, CompanionStore
from .companion_authorization import authorize_companion_project
from .config import Settings
from .data_operations import data_operation
from .models import utc_now
from .permissions import PermissionBroker
from .push_store import PushRecord, PushStore, PushWake
from .push_transport import PushSubscription, WebPushAdapter
from .storage import FileLedger

logger = logging.getLogger(__name__)


def _anchor(kind: str, item_id: str) -> str:
    return hashlib.sha256(f"{kind}:{item_id}".encode()).hexdigest()


class PushDeliveryService:
    def __init__(
        self, settings: Settings, store: PushStore, adapter: WebPushAdapter,
        companions: CompanionStore, ledger: FileLedger, access: ProjectAccessStore,
        permissions: PermissionBroker, notifications: NotificationStore,
    ):
        self.settings, self.store, self.adapter = settings, store, adapter
        self.companions, self.ledger, self.access = companions, ledger, access
        self.permissions, self.notifications = permissions, notifications
        self._task: asyncio.Task | None = None
        self._slots = asyncio.Semaphore(4)

    def status(self, device_id: str) -> dict:
        record = self.store.get(device_id)
        reason = self.adapter.reason or None
        subscribed = False
        if record:
            reason = reason or record.disabled_reason
            if record.public_key != self.adapter.public_key:
                reason = reason or "application_key_changed"
            elif record.subscription and record.subscription.expired(utc_now()):
                reason = reason or "subscription_expired"
            elif record.subscription and self.adapter.enabled and not record.disabled_reason:
                try:
                    self.adapter.validate(record.subscription)
                    subscribed = True
                except ValueError:
                    reason = reason or "subscription_unavailable"
        return {
            "enabled": self.adapter.enabled, "public_key": self.adapter.public_key,
            "subscribed": subscribed,
            "subscription_fingerprint": (
                record.subscription.fingerprint if subscribed and record and record.subscription
                else None
            ),
            "reason": reason,
        }

    def subscribe(self, device_id: str, subscription: PushSubscription) -> dict:
        self.adapter.validate(subscription)
        self.store.subscribe(device_id, subscription, self.adapter.public_key)
        return self.status(device_id)

    def _projects(self, device_id: str) -> set[str]:
        record = self.companions.approved_record(device_id)
        projects: set[str] = set()
        for project_id in record.project_ids:
            try:
                authorize_companion_project(
                    self.settings, self.ledger, self.access, record,
                    ProjectPermission.PROJECT_WRITE, project_id,
                )
                projects.add(project_id)
            except CompanionError:
                continue
        if not projects:
            raise CompanionError("device project access was revoked")
        return projects

    def _sources(self, projects: set[str], now: datetime) -> dict[str, datetime]:
        sources = {
            _anchor("notification", item.id): item.created_at
            for item in self.notifications.list(unread_only=True, limit=200, project_ids=projects)
        }
        for item in self.permissions.list_pending():
            if (item.project_id in projects and item.state == "pending"
                    and (item.expires_at is None or item.expires_at > now)):
                sources[_anchor("permission", item.id)] = item.created_at
        return sources

    @data_operation()
    async def _deliver(self, device_id: str, now: datetime) -> None:
        async with self._slots:
            with self.store.delivery_lease(device_id) as owned:
                if not owned:
                    return
                record = self.store.get(device_id)
                if not record or not record.subscription or record.disabled_reason:
                    return
                try:
                    projects = self._projects(device_id)
                except CompanionError:
                    self.store.unsubscribe(
                        device_id, "device_authority_revoked", generation=record.generation,
                    )
                    return
                if (record.public_key != self.adapter.public_key
                        or record.subscription.expired(now)):
                    self.store.unsubscribe(
                        device_id, "subscription_expired_or_key_changed",
                        generation=record.generation,
                    )
                    return
                sources = self._sources(projects, now)
                wake = record.wake
                if wake and wake.state in {"pending", "attempting"}:
                    if not set(wake.anchors).intersection(sources):
                        wake.state, wake.reason = "cancelled", "no_pending_updates"
                    elif wake.created_at + timedelta(minutes=5) <= now:
                        wake.state, wake.reason = "failed", "wake_expired"
                    elif wake.attempts >= 5:
                        wake.state, wake.reason = "failed", "retry_budget_exhausted"
                if wake is None or wake.state not in {"pending", "attempting"}:
                    fresh = {
                        key: created for key, created in sources.items()
                        if key not in record.known and created >= record.known_floor
                        and created >= now - timedelta(days=1)
                    }
                    if fresh:
                        # One generic wake covers a burst; do not turn every inbox row into a nag.
                        wake = PushWake(
                            anchors=sorted(fresh, key=fresh.get, reverse=True)[:200],
                            created_at=now, next_attempt_at=now,
                        )
                        record.wake = wake
                        self._remember(record, fresh, now)
                if not self.store.save(record):
                    return
                if not wake or wake.state not in {"pending", "attempting"}:
                    return
                if wake.next_attempt_at > now:
                    return
                if record.retry_not_before and record.retry_not_before > now:
                    return
                if record.last_attempt_at and (
                    record.last_attempt_at + timedelta(
                        seconds=self.settings.push_min_interval_seconds,
                    )
                    > now
                ):
                    return
                day = now.date().isoformat()
                if record.attempt_day != day:
                    record.attempt_day, record.daily_attempts = day, 0
                if record.daily_attempts >= self.settings.push_max_attempts_per_day:
                    wake.state, wake.reason = "failed", "daily_attempt_budget_exhausted"
                    self.store.save(record)
                    return
                # Attempt is durable BEFORE network. A crash spends an attempt; it never
                # produces an unbounded retry loop. Stable Topic coalesces ambiguous retries.
                wake.state = "attempting"
                wake.attempts += 1
                record.last_attempt_at = now
                record.daily_attempts += 1
                wake.next_attempt_at = now + timedelta(seconds=min(15 * 2 ** wake.attempts, 300))
                if not self.store.save(record):
                    return
                try:
                    # Revalidate after preparing the durable attempt, before any external send.
                    active = self._sources(self._projects(device_id), now)
                    if not set(wake.anchors).intersection(active):
                        wake.state, wake.reason = "cancelled", "no_pending_updates"
                        self.store.save(record)
                        return
                except CompanionError:
                    self.store.unsubscribe(
                        device_id, "device_authority_revoked", generation=record.generation,
                    )
                    return
                latest = self.store.get(device_id)
                if latest is None or latest.generation != record.generation:
                    return
                topic = hashlib.sha256(wake.id.encode()).hexdigest()[:32]
                started_at = monotonic()
                outcome = await self.adapter.send(record.subscription, topic)
                finished_at = now + timedelta(seconds=max(0, monotonic() - started_at))
                wake.reason = outcome.reason
                if outcome.status == "retry" and outcome.retry_after > 0:
                    # A new inbox row, resolved approval or renewed subscription
                    # must not bypass a service-requested pause for this device.
                    record.retry_not_before = finished_at + timedelta(seconds=outcome.retry_after)
                if outcome.status == "accepted":
                    wake.state = "accepted"
                    record.last_accepted_at = now
                    record.retry_not_before = None
                elif outcome.status == "disabled":
                    wake.state = "failed"
                    record.subscription = None
                    record.disabled_reason = outcome.reason
                elif outcome.status == "retry" and wake.attempts < 5:
                    wake.state = "pending"
                    wake.next_attempt_at = max(
                        wake.next_attempt_at, finished_at + timedelta(seconds=outcome.retry_after),
                    )
                else:
                    wake.state = "failed"
                self.store.save(record)

    @staticmethod
    def _remember(record: PushRecord, fresh: dict[str, datetime], now: datetime) -> None:
        entries = {**record.known, **fresh}
        ordered = sorted(entries, key=entries.get, reverse=True)
        if len(ordered) > 1_000:
            record.known_floor = max(
                record.known_floor, entries[ordered[1_000]] + timedelta(microseconds=1),
            )
        record.known_floor = max(record.known_floor, now - timedelta(days=1))
        record.known = {key: entries[key] for key in ordered[:1_000]
                        if entries[key] >= record.known_floor}

    @data_operation()
    async def tick(self, *, now: datetime | None = None) -> None:
        if not self.adapter.enabled:
            return
        moment = now or utc_now()

        async def one(device_id: str):
            try:
                await self._deliver(device_id, moment)
            except (OSError, ValueError):
                # A corrupt device record must not stop other phones; no endpoint or key logs.
                logger.warning("Push record unavailable for device %s", device_id)

        await asyncio.gather(*(one(device_id) for device_id in self.store.list_ids()))

    async def start(self) -> None:
        if self._task is not None or not self.adapter.enabled:
            return

        async def loop():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.warning("Push wake scan failed; retrying without logging capabilities")
                await asyncio.sleep(self.settings.push_worker_interval_seconds)

        self._task = asyncio.create_task(loop(), name="companion-push")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
