"""Mailbox-wide Graph subscriptions with project-bound, durable event delivery."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .event_triggers import EventTrigger, EventTriggerStore
from .outlook import _MESSAGE_ID, GRAPH_API, OutlookError, OutlookService
from .storage import FileLedger

LOGGER = logging.getLogger(__name__)
OUTLOOK_NOTIFICATIONS_PATH = "/api/connectors/{connector_id}/outlook/notifications"
OUTLOOK_NOTIFICATIONS_RE = re.compile(
    r"/api/connectors/[A-Za-z0-9_-]+/outlook/notifications"
)
MAX_NOTIFICATION_BODY = 64_000
_GRAPH_ID = re.compile(r"^[A-Za-z0-9-]{1,160}$")
_STATE_KEY = "outlook-subscription-state:"


class OutlookWatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector_id: str
    connector_revision: int
    tenant_id: str | None
    user_id: str
    email: str
    notification_url: str
    status: Literal["attempting", "uncertain", "active", "removed"]
    subscription_id: str | None = None
    expiration_at: datetime | None = None
    gap: bool = False
    reauthorize: bool = False
    updated_at: datetime


class OutlookEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str
    trigger_id: str
    connector_id: str
    connector_revision: int
    tenant_id: str | None
    user_id: str
    subscription_id: str | None = None


class OutlookNotification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector_id: str
    subscription_id: str
    message_id: str
    targets: list[str]
    completed: list[str] = Field(default_factory=list)
    payload: dict[str, str] | None = None
    received_at: float
    finished_at: float | None = None


class OutlookSubscriptions:
    def __init__(
        self, service: OutlookService, ledger: FileLedger, triggers: EventTriggerStore,
        dispatch: Callable[
            [EventTrigger, str, bytes, dict[str, str]], Awaitable[dict[str, object]]
        ],
    ) -> None:
        self.service = service
        self.ledger = ledger
        self.triggers = triggers
        self.dispatch = dispatch
        self.root = ledger.root / "outlook-subscriptions"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._task: asyncio.Task[None] | None = None

    def _watch_path(self, connector_id: str) -> Path:
        return self.root / f"{self.ledger._safe_id(connector_id)}.json"

    def _watch(self, connector_id: str) -> OutlookWatch | None:
        path = self._watch_path(connector_id)
        if not path.is_file():
            return None
        watch = OutlookWatch.model_validate_json(path.read_text("utf-8"))
        if watch.connector_id != connector_id:
            raise OutlookError("OUTLOOK_WATCH_INVALID", "Outlook watch binding is invalid", 500)
        return watch

    def _save(self, watch: OutlookWatch) -> None:
        self.ledger._atomic_write(self._watch_path(watch.connector_id), watch)

    def _enrollment_path(self, trigger: EventTrigger) -> Path:
        return (
            self.ledger.projects_dir / self.ledger._safe_id(trigger.project_id)
            / "event-triggers" / "outlook-enrollments"
            / f"{self.ledger._safe_id(trigger.id)}.json"
        )

    def _enrollment(self, trigger: EventTrigger) -> OutlookEnrollment | None:
        path = self._enrollment_path(trigger)
        if not path.is_file():
            return None
        value = OutlookEnrollment.model_validate_json(path.read_text("utf-8"))
        if value.project_id != trigger.project_id or value.trigger_id != trigger.id:
            raise OutlookError("OUTLOOK_ENROLLMENT_INVALID", "Outlook enrollment is invalid", 500)
        return value

    def _enroll(self, trigger: EventTrigger, watch: OutlookWatch) -> None:
        self.ledger._atomic_write(self._enrollment_path(trigger), OutlookEnrollment(
            project_id=trigger.project_id, trigger_id=trigger.id,
            connector_id=watch.connector_id,
            connector_revision=watch.connector_revision,
            tenant_id=watch.tenant_id, user_id=watch.user_id,
            subscription_id=watch.subscription_id,
        ))

    def _matches(self, watch: OutlookWatch, tenant_id: str | None) -> bool:
        try:
            token = self.service._token(watch.connector_id, tenant_id)
        except OutlookError:
            return False
        return (
            token.tenant_id == watch.tenant_id
            and token.connector_revision == watch.connector_revision
            and token.user_id == watch.user_id and token.email == watch.email
            and watch.notification_url == self.notification_url(watch.connector_id)
        )

    def notification_url(self, connector_id: str) -> str | None:
        settings = self.service.settings
        if settings.auth_mode != "trusted_gateway" or not settings.event_public_origin:
            return None
        return settings.event_public_origin + OUTLOOK_NOTIFICATIONS_PATH.format(
            connector_id=connector_id,
        )

    def status(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        _user, email = self.service.account(connector_id, tenant_id)
        watch = self._watch(connector_id)
        enrollment = self._enrollment(trigger)
        enrolled = bool(
            watch and enrollment
            and enrollment.connector_id == connector_id
            and enrollment.connector_revision == watch.connector_revision
            and enrollment.tenant_id == watch.tenant_id
            and enrollment.user_id == watch.user_id
            and enrollment.subscription_id == watch.subscription_id
        )
        valid = bool(watch and self._matches(watch, tenant_id))
        state = (
            "unconfigured" if self.notification_url(connector_id) is None
            else "not_registered" if not enrolled
            else "changed" if not valid
            else "stale" if watch.gap
            else "expired" if watch.status == "active" and (
                watch.expiration_at is None
                or watch.expiration_at <= datetime.now(UTC)
            )
            else watch.status
        )
        return {
            "email": email, "notification_url": self.notification_url(connector_id),
            "status": state,
            "gap": bool(watch and watch.gap and valid),
            "subscription_id": watch.subscription_id if enrolled else None,
            "expiration_at": watch.expiration_at if enrolled else None,
        }

    @staticmethod
    def _expiration(raw: Any) -> datetime:
        try:
            if not isinstance(raw, str):
                raise ValueError
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed <= datetime.now(UTC):
                raise ValueError
            if parsed > datetime.now(UTC) + timedelta(days=7):
                raise ValueError
            return parsed
        except ValueError:
            raise OutlookError(
                "OUTLOOK_SUBSCRIPTION_INVALID", "Graph returned an invalid expiration", 502
            ) from None

    @contextmanager
    def _remote_lock(self, connector_id: str) -> Iterator[None]:
        """Cross-worker lock held across Graph calls; a contender fails fast."""
        path = self.root / f"{self.ledger._safe_id(connector_id)}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_BUSY", "Outlook subscription operation is in progress"
                ) from None
            yield
        finally:
            os.close(fd)

    async def register(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        url = self.notification_url(connector_id)
        if url is None:
            raise OutlookError("OUTLOOK_ORIGIN_REQUIRED", "Configure a public HTTPS event origin")
        token = await self.service._access(connector_id, tenant_id)
        if token.tenant_id != tenant_id or trigger.outlook_connector_id != connector_id:
            raise OutlookError("OUTLOOK_TRIGGER_INVALID", "Outlook trigger binding is invalid", 403)
        with self._remote_lock(connector_id):
            prior = self._watch(connector_id)
            if prior is not None:
                if not self._matches(prior, tenant_id):
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_CHANGED",
                        "Outlook mailbox or public origin changed; reconcile the old subscription",
                    )
                if prior.status in {"attempting", "uncertain"}:
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_UNCERTAIN",
                        "Check Microsoft Graph for an existing subscription before retrying",
                    )
                if prior.status == "active":
                    if prior.expiration_at is None or prior.subscription_id is None:
                        raise OutlookError(
                            "OUTLOOK_SUBSCRIPTION_INVALID", "Outlook subscription is invalid"
                        )
                    if prior.expiration_at < datetime.now(UTC) + timedelta(days=1):
                        try:
                            await self._renew(prior, token.access_token)
                        except OutlookError as exc:
                            if exc.code != "OUTLOOK_NOT_FOUND":
                                raise
                            prior = prior.model_copy(update={
                                "status": "removed", "gap": True,
                                "updated_at": datetime.now(UTC),
                            })
                            self._save(prior)
                        else:
                            prior = self._watch(connector_id)
                    if prior.status == "active":
                        self._enroll(trigger, prior)
                        return self.status(connector_id, tenant_id, trigger)
                if prior.subscription_id and self._pending_for(prior):
                    raise OutlookError(
                        "OUTLOOK_QUEUE_PENDING",
                        "Finish queued Outlook notifications before replacing the subscription",
                    )
            state = secrets.token_urlsafe(32)
            pending = OutlookWatch(
                connector_id=connector_id, connector_revision=token.connector_revision,
                tenant_id=tenant_id, user_id=token.user_id, email=token.email,
                notification_url=url, status="attempting", gap=prior.gap if prior else False,
                updated_at=datetime.now(UTC),
            )
            assert self.service.connectors.vault is not None
            self.service.connectors.vault.set(_STATE_KEY + connector_id, state)
            self._save(pending)
            self._enroll(trigger, pending)
            try:
                result = await self.service._json(
                    "POST", GRAPH_API + "/subscriptions",
                    headers={
                        "Authorization": f"Bearer {token.access_token}",
                        "Prefer": 'IdType="ImmutableId"',
                    },
                    payload={
                        "changeType": "created",
                        "notificationUrl": url,
                        "lifecycleNotificationUrl": url,
                        "resource": "me/mailFolders('inbox')/messages",
                        "expirationDateTime": (
                            datetime.now(UTC) + timedelta(days=6)
                        ).isoformat(),
                        "clientState": state,
                    },
                )
                subscription_id = result.get("id")
                if not isinstance(subscription_id, str) or not _GRAPH_ID.fullmatch(subscription_id):
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_INVALID",
                        "Graph returned an invalid subscription", 502,
                    )
                expiration = self._expiration(result.get("expirationDateTime"))
                if not self._matches(pending, tenant_id):
                    raise OutlookError("OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed")
            except Exception:
                self._save(pending.model_copy(update={
                    "status": "uncertain", "updated_at": datetime.now(UTC),
                }))
                raise
            active = pending.model_copy(update={
                "status": "active", "subscription_id": subscription_id,
                "expiration_at": expiration, "updated_at": datetime.now(UTC),
            })
            self._save(active)
            self._enroll(trigger, active)
        return self.status(connector_id, tenant_id, trigger)

    async def _renew(self, watch: OutlookWatch, access_token: str) -> None:
        if watch.subscription_id is None:
            raise OutlookError("OUTLOOK_SUBSCRIPTION_INVALID", "Outlook subscription is invalid")
        result = await self.service._json(
            "PATCH", GRAPH_API + "/subscriptions/" + watch.subscription_id,
            headers={"Authorization": f"Bearer {access_token}"},
            payload={"expirationDateTime": (
                datetime.now(UTC) + timedelta(days=6)
            ).isoformat()},
        )
        expiration = self._expiration(result.get("expirationDateTime"))
        self._save(watch.model_copy(update={
            "expiration_at": expiration, "reauthorize": False,
            "updated_at": datetime.now(UTC),
        }))

    async def reconcile(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        """Resolve an uncertain POST using this app's exact Graph subscription list."""
        token = await self.service._access(connector_id, tenant_id)
        _revision, credentials = self.service._connector(connector_id)
        with self._remote_lock(connector_id):
            watch = self._watch(connector_id)
            if (
                watch is None or watch.status not in {"attempting", "uncertain"}
                or trigger.outlook_connector_id != connector_id
                or self._enrollment(trigger) is None
                or not self._matches(watch, tenant_id)
            ):
                raise OutlookError(
                    "OUTLOOK_RECONCILE_INVALID", "No matching uncertain subscription"
                )
            url = GRAPH_API + "/subscriptions"
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            for _page in range(10):
                if url in seen:
                    raise OutlookError("OUTLOOK_SUBSCRIPTION_INVALID", "Graph paging loop", 502)
                seen.add(url)
                result = await self.service._json(
                    "GET", url, headers={"Authorization": f"Bearer {token.access_token}"},
                )
                items = result.get("value")
                if not isinstance(items, list) or len(items) > 1000:
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription list is invalid", 502
                    )
                for item in items:
                    if not isinstance(item, dict):
                        raise OutlookError(
                            "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription list is invalid",
                            502,
                        )
                    if (
                        item.get("applicationId") == credentials.client_id
                        and item.get("notificationUrl") == watch.notification_url
                        and item.get("lifecycleNotificationUrl") == watch.notification_url
                        and isinstance(item.get("resource"), str)
                        and item["resource"].lstrip("/").casefold()
                        == "me/mailfolders('inbox')/messages"
                        and item.get("changeType") == "created"
                    ):
                        found.append(item)
                next_url = result.get("@odata.nextLink")
                if next_url is None:
                    break
                if not isinstance(next_url, str) or not 1 <= len(next_url) <= 4096:
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription cursor is invalid",
                        502,
                    )
                parts = urlsplit(next_url)
                if (
                    parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
                    or parts.path != "/v1.0/subscriptions" or parts.fragment
                    or not parts.query or any(ord(char) < 33 for char in next_url)
                ):
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription cursor is invalid",
                        502,
                    )
                url = next_url
            else:
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription list is incomplete", 502
                )
            if len(found) > 1:
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_AMBIGUOUS",
                    "Multiple matching Graph subscriptions require manual review",
                )
            if found:
                remote = found[0]
                identifier = remote.get("id")
                if not isinstance(identifier, str) or not _GRAPH_ID.fullmatch(identifier):
                    raise OutlookError(
                        "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription ID is invalid", 502
                    )
                updated = watch.model_copy(update={
                    "status": "active", "subscription_id": identifier,
                    "expiration_at": self._expiration(remote.get("expirationDateTime")),
                    "updated_at": datetime.now(UTC),
                })
                self._save(updated)
                self._enroll(trigger, updated)
            else:
                # A lost POST might have missed mail. Keep that gap visible.
                self._save(watch.model_copy(update={
                    "status": "removed", "gap": True,
                    "updated_at": datetime.now(UTC),
                }))
        return self.status(connector_id, tenant_id, trigger)

    async def stop(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        token = await self.service._access(connector_id, tenant_id)
        with self._remote_lock(connector_id):
            watch = self._watch(connector_id)
            enrollment = self._enrollment(trigger)
            if watch is None or enrollment is None:
                return self.status(connector_id, tenant_id, trigger)
            if enrollment.subscription_id != watch.subscription_id:
                self._enrollment_path(trigger).unlink(missing_ok=True)
                return self.status(connector_id, tenant_id, trigger)
            if not self._matches(watch, tenant_id):
                raise OutlookError("OUTLOOK_SUBSCRIPTION_CHANGED", "Outlook subscription changed")
            others = [
                item for item in self._triggers(watch)
                if item.id != trigger.id
            ]
            if others:
                self._enrollment_path(trigger).unlink(missing_ok=True)
                return self.status(connector_id, tenant_id, trigger)
            if watch.status in {"attempting", "uncertain"}:
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_UNCERTAIN",
                    "Check Microsoft Graph before removing an uncertain subscription",
                )
            if watch.subscription_id and self._pending_for(watch):
                raise OutlookError(
                    "OUTLOOK_QUEUE_PENDING",
                    "Finish queued Outlook notifications before stopping the subscription",
                )
            if watch.status == "active" and watch.subscription_id:
                try:
                    await self.service._json(
                        "DELETE", GRAPH_API + "/subscriptions/" + watch.subscription_id,
                        headers={"Authorization": f"Bearer {token.access_token}"},
                    )
                except OutlookError as exc:
                    if exc.code != "OUTLOOK_NOT_FOUND":
                        raise
            self._enrollment_path(trigger).unlink(missing_ok=True)
            self._watch_path(connector_id).unlink(missing_ok=True)
            assert self.service.connectors.vault is not None
            self.service.connectors.vault.delete(_STATE_KEY + connector_id)
        return self.status(connector_id, tenant_id, trigger)

    def _triggers(self, watch: OutlookWatch) -> list[EventTrigger]:
        result = []
        for path in self.ledger.projects_dir.glob(
            "*/event-triggers/outlook-enrollments/*.json"
        ):
            try:
                item = OutlookEnrollment.model_validate_json(path.read_text("utf-8"))
                if (
                    item.connector_id != watch.connector_id
                    or item.connector_revision != watch.connector_revision
                    or item.tenant_id != watch.tenant_id
                    or item.user_id != watch.user_id
                    or item.subscription_id != watch.subscription_id
                ):
                    continue
                trigger = self.triggers.get(item.project_id, item.trigger_id)
                if trigger.enabled and trigger.outlook_connector_id == watch.connector_id:
                    result.append(trigger)
            except (KeyError, ValueError, RuntimeError):
                continue
        return result

    def _queue_path(self, connector_id: str, subscription_id: str, message_id: str) -> Path:
        digest = hashlib.sha256((subscription_id + "\0" + message_id).encode()).hexdigest()
        return self.root / self.ledger._safe_id(connector_id) / f"{digest}.json"

    def _pending_for(self, watch: OutlookWatch) -> bool:
        for path in (self.root / self.ledger._safe_id(watch.connector_id)).glob("*.json"):
            notice = OutlookNotification.model_validate_json(path.read_text("utf-8"))
            if notice.subscription_id == watch.subscription_id and notice.finished_at is None:
                return True
        return False

    def receive(self, connector_id: str, body: bytes) -> dict[str, int]:
        try:
            value = json.loads(body)
            notifications = value["value"]
            if not isinstance(notifications, list) or len(notifications) > 100:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise OutlookError(
                "OUTLOOK_NOTIFICATION_INVALID", "Invalid Graph notification", 400
            ) from None
        watch = self._watch(connector_id)
        if watch is None or watch.status not in {"attempting", "active"}:
            return {"accepted": 0}
        secret = self.service.connectors.vault.get(_STATE_KEY + connector_id)
        if not secret or not self._matches(watch, watch.tenant_id):
            return {"accepted": 0}
        count = 0
        for item in notifications:
            if not isinstance(item, dict):
                continue
            state = item.get("clientState")
            sub_id = item.get("subscriptionId")
            if (
                not isinstance(state, str) or not hmac.compare_digest(state, secret)
                or not isinstance(sub_id, str) or not _GRAPH_ID.fullmatch(sub_id)
                or (watch.subscription_id is not None and sub_id != watch.subscription_id)
            ):
                continue
            lifecycle = item.get("lifecycleEvent")
            if lifecycle in {"missed", "subscriptionRemoved", "reauthorizationRequired"}:
                with self.ledger._lock_for(f"outlook-watch:{connector_id}"):
                    latest = self._watch(connector_id)
                    if latest and latest.subscription_id == sub_id:
                        self._save(latest.model_copy(update={
                            "gap": latest.gap or lifecycle in {"missed", "subscriptionRemoved"},
                            "status": (
                                "removed" if lifecycle == "subscriptionRemoved" else latest.status
                            ),
                            "reauthorize": lifecycle == "reauthorizationRequired",
                            "updated_at": datetime.now(UTC),
                        }))
                count += 1
                continue
            data = item.get("resourceData")
            message_id = data.get("id") if isinstance(data, dict) else None
            if (
                item.get("changeType") != "created" or not isinstance(message_id, str)
                or not _MESSAGE_ID.fullmatch(message_id)
            ):
                continue
            targets = [f"{t.project_id}:{t.id}" for t in self._triggers(watch)]
            if len(targets) > 1000:
                raise OutlookError("OUTLOOK_QUEUE_FULL", "Too many Outlook triggers", 503)
            if not targets:
                continue
            path = self._queue_path(connector_id, sub_id, message_id)
            with self.ledger._lock_for(f"outlook-queue:{path.stem}"):
                if not path.exists():
                    if len(list(path.parent.glob("*.json"))) >= 10_000:
                        raise OutlookError(
                            "OUTLOOK_QUEUE_FULL", "Outlook notification queue is full", 503
                        )
                    self.ledger._atomic_write(path, OutlookNotification(
                        connector_id=connector_id, subscription_id=sub_id,
                        message_id=message_id, targets=targets, received_at=time.time(),
                    ))
            count += 1
        return {"accepted": count}

    @contextmanager
    def _item_lock(self, path: Path) -> Iterator[bool]:
        # Fixed lock buckets keep lock-file count bounded as notifications age out.
        locks = path.parent / ".locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        lock = locks / (path.stem[:2] + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    async def process_pending(self) -> None:
        processed = 0
        for path in self.root.glob("*/*.json"):
            if processed >= 20:
                break
            with self._item_lock(path) as acquired:
                if not acquired or not path.exists():
                    continue
                try:
                    notice = OutlookNotification.model_validate_json(path.read_text("utf-8"))
                    if notice.finished_at is not None:
                        if notice.finished_at < time.time() - 30 * 86_400:
                            path.unlink(missing_ok=True)
                        continue
                    watch = self._watch(notice.connector_id)
                    if watch is not None and watch.status in {"attempting", "uncertain"}:
                        continue
                    if (
                        watch is None or watch.subscription_id != notice.subscription_id
                        or not self._matches(watch, watch.tenant_id)
                    ):
                        self.ledger._atomic_write(path, notice.model_copy(update={
                            "message_id": "", "targets": [], "completed": [],
                            "payload": None, "finished_at": time.time(),
                        }))
                        continue
                    live = {f"{item.project_id}:{item.id}" for item in self._triggers(watch)}
                    if not any(target in live for target in notice.targets):
                        self.ledger._atomic_write(path, notice.model_copy(update={
                            "message_id": "", "targets": [], "completed": [],
                            "payload": None, "finished_at": time.time(),
                        }))
                        continue
                    if notice.payload is None:
                        payload = await self.service.read_inbox_message(
                            notice.connector_id, watch.tenant_id, notice.message_id,
                        )
                        if payload is None:
                            self.ledger._atomic_write(path, notice.model_copy(update={
                                "message_id": "", "targets": [], "completed": [],
                                "finished_at": time.time(),
                            }))
                            continue
                        notice = notice.model_copy(update={"payload": payload})
                        self.ledger._atomic_write(path, notice)
                    event_id = "outlook:" + hashlib.sha256(notice.message_id.encode()).hexdigest()
                    body = json.dumps(
                        notice.payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    for target in notice.targets:
                        if target in notice.completed:
                            continue
                        project_id, trigger_id = target.split(":", 1)
                        try:
                            trigger = self.triggers.get(project_id, trigger_id)
                        except (KeyError, ValueError, RuntimeError):
                            trigger = None
                        if trigger is not None and trigger.enabled and (
                            trigger.outlook_connector_id == notice.connector_id
                        ) and self._enrollment(trigger) is not None:
                            result = await self.dispatch(trigger, event_id, body, notice.payload)
                            if result.get("status") != "accepted":
                                raise OutlookError(
                                    "OUTLOOK_DISPATCH_UNCERTAIN",
                                    "Outlook Run dispatch is uncertain",
                                )
                        notice = notice.model_copy(update={
                            "completed": [*notice.completed, target],
                        })
                        self.ledger._atomic_write(path, notice)
                    self.ledger._atomic_write(path, notice.model_copy(update={
                        "message_id": "", "targets": [], "completed": [],
                        "payload": None, "finished_at": time.time(),
                    }))
                    processed += 1
                except (OutlookError, HTTPException, ValidationError, OSError):
                    LOGGER.exception("Outlook notification processing failed for %s", path.name)

    async def renew_due(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                watch = self._watch(path.stem)
                if (
                    watch is None or watch.status != "active"
                    or not self._matches(watch, watch.tenant_id)
                    or not self._triggers(watch)
                    or not (watch.reauthorize or watch.expiration_at is None
                            or watch.expiration_at < datetime.now(UTC) + timedelta(days=1))
                ):
                    continue
                token = await self.service._access(watch.connector_id, watch.tenant_id)
                with self._remote_lock(watch.connector_id):
                    current = self._watch(watch.connector_id)
                    if current and current.subscription_id == watch.subscription_id:
                        try:
                            await self._renew(current, token.access_token)
                        except OutlookError as exc:
                            if exc.code != "OUTLOOK_NOT_FOUND":
                                raise
                            self._save(current.model_copy(update={
                                "status": "removed", "gap": True,
                                "updated_at": datetime.now(UTC),
                            }))
            except (OutlookError, ValueError, OSError):
                LOGGER.exception("Outlook subscription renewal failed for %s", path.stem)

    async def start(self) -> None:
        if self._task is not None:
            return

        async def worker() -> None:
            cycle = 0
            while True:
                try:
                    await self.process_pending()
                    if cycle % 720 == 0:
                        await self.renew_due()
                except Exception:
                    LOGGER.exception("Outlook subscription worker failed")
                cycle += 1
                await asyncio.sleep(5)

        self._task = asyncio.create_task(worker(), name="outlook-subscriptions")

    async def stop_worker(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
