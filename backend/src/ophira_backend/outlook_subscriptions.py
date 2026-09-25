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
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

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
_DELTA_KEY = "outlook-subscription-delta:"
_DELTA_PAGE_LIMIT = 100
_DELTA_ITEM_LIMIT = 5_000
_DELTA_PATH = "/me/mailFolders/{folder_id}/messages/delta"


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
    remote_unverified: bool = False
    reauthorize: bool = False
    delta_from: datetime | None = None
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
    enrolled_at: datetime | None = None


class OutlookDeltaCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector_revision: int
    tenant_id: str | None
    user_id: str
    folder_id: str
    url: str
    synced_at: datetime


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
        previous = self._enrollment(trigger)
        enrolled_at = (
            previous.enrolled_at
            if previous is not None
            and previous.connector_id == watch.connector_id
            and previous.connector_revision == watch.connector_revision
            and previous.tenant_id == watch.tenant_id
            and previous.user_id == watch.user_id
            else None
        )
        self.ledger._atomic_write(self._enrollment_path(trigger), OutlookEnrollment(
            project_id=trigger.project_id, trigger_id=trigger.id,
            connector_id=watch.connector_id,
            connector_revision=watch.connector_revision,
            tenant_id=watch.tenant_id, user_id=watch.user_id,
            subscription_id=watch.subscription_id,
            enrolled_at=enrolled_at or (
                watch.delta_from if watch.status == "attempting" and watch.delta_from
                else datetime.now(UTC)
            ),
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

    @staticmethod
    def _delta_url(raw: Any, folder_id: str) -> str:
        if (
            not isinstance(raw, str) or not 1 <= len(raw) <= 8192
            or any(ord(char) < 33 or ord(char) == 127 for char in raw)
        ):
            raise OutlookError("OUTLOOK_DELTA_INVALID", "Graph delta cursor is invalid", 502)
        try:
            parts = urlsplit(raw)
            path = unquote(parts.path)
            allowed_paths = {
                f"/v1.0/me/mailFolders/{folder_id}/messages/delta",
                f"/v1.0/me/mailFolders('{folder_id}')/messages/delta",
                "/v1.0/me/mailFolders/inbox/messages/delta",
                "/v1.0/me/mailFolders('inbox')/messages/delta",
            }
            pairs = parse_qsl(
                parts.query, keep_blank_values=True, strict_parsing=True,
                max_num_fields=8,
            )
            params = dict(pairs)
            token_keys = {"$skiptoken", "$deltatoken"} & set(params)
            if (
                parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
                or path not in allowed_paths or parts.fragment
                or len(params) != len(pairs) or len(token_keys) != 1
                or set(params) - {
                    "$skiptoken", "$deltatoken", "$select", "$filter", "$top", "changeType",
                }
                or not 1 <= len(params[next(iter(token_keys))]) <= 8000
                or ("changeType" in params and params["changeType"] != "created")
                or ("$select" in params and params["$select"] != "id,receivedDateTime")
                or ("$filter" in params and not params["$filter"].startswith(
                    "receivedDateTime ge "
                ))
            ):
                raise ValueError
        except ValueError:
            raise OutlookError(
                "OUTLOOK_DELTA_INVALID", "Graph delta cursor is invalid", 502,
            ) from None
        return raw

    def _delta_cursor(self, watch: OutlookWatch) -> OutlookDeltaCursor | None:
        assert self.service.connectors.vault is not None
        raw = self.service.connectors.vault.get(_DELTA_KEY + watch.connector_id)
        if raw is None:
            return None
        try:
            cursor = OutlookDeltaCursor.model_validate_json(raw)
            if (
                cursor.connector_revision != watch.connector_revision
                or cursor.tenant_id != watch.tenant_id
                or cursor.user_id != watch.user_id
                or not _MESSAGE_ID.fullmatch(cursor.folder_id)
                or cursor.synced_at.tzinfo is None
            ):
                raise ValueError
            self._delta_url(cursor.url, cursor.folder_id)
            return cursor
        except (ValidationError, ValueError):
            raise OutlookError(
                "OUTLOOK_DELTA_INVALID", "Outlook delta cursor is invalid", 409,
            ) from None

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
        try:
            cursor = self._delta_cursor(watch) if watch and valid else None
        except OutlookError:
            cursor = None
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
            "remote_unverified": bool(watch and watch.remote_unverified and valid),
            "subscription_id": watch.subscription_id if enrolled else None,
            "expiration_at": watch.expiration_at if enrolled else None,
            "delta_ready": bool(cursor),
            "delta_last_synced_at": cursor.synced_at if cursor else None,
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
                remote_unverified=prior.remote_unverified if prior else False,
                delta_from=(
                    prior.delta_from if prior and prior.gap else datetime.now(UTC)
                ),
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

    async def _matching_remote(
        self, access_token: str, application_id: str, notification_url: str,
    ) -> list[dict[str, Any]]:
        url = GRAPH_API + "/subscriptions"
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _page in range(10):
            if url in seen:
                raise OutlookError("OUTLOOK_SUBSCRIPTION_INVALID", "Graph paging loop", 502)
            seen.add(url)
            result = await self.service._json(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"},
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
                    item.get("applicationId") == application_id
                    and item.get("notificationUrl") == notification_url
                    and item.get("lifecycleNotificationUrl") == notification_url
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
                    "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription cursor is invalid", 502
                )
            parts = urlsplit(next_url)
            if (
                parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
                or parts.path != "/v1.0/subscriptions" or parts.fragment
                or not parts.query or any(ord(char) < 33 for char in next_url)
            ):
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription cursor is invalid", 502
                )
            url = next_url
        else:
            raise OutlookError(
                "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription list is incomplete", 502
            )
        return found

    async def reconcile(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        """Resolve a lost POST or explicitly replace a changed connector binding."""
        token = await self.service._access(connector_id, tenant_id)
        _revision, credentials = self.service._connector(connector_id)
        with self._remote_lock(connector_id):
            watch = self._watch(connector_id)
            if (
                watch is None or trigger.outlook_connector_id != connector_id
                or self._enrollment(trigger) is None
                or watch.tenant_id != tenant_id or token.tenant_id != tenant_id
            ):
                raise OutlookError(
                    "OUTLOOK_RECONCILE_INVALID", "No matching Outlook subscription"
                )
            same = self._matches(watch, tenant_id)
            if same and watch.status not in {"attempting", "uncertain"}:
                raise OutlookError(
                    "OUTLOOK_RECONCILE_INVALID", "Outlook subscription does not need recovery"
                )
            if not same and watch.subscription_id and self._pending_for(watch):
                raise OutlookError(
                    "OUTLOOK_QUEUE_PENDING", "Finish queued notifications before recovery"
                )
            new_url = self.notification_url(connector_id)
            if not same and new_url is None:
                raise OutlookError(
                    "OUTLOOK_ORIGIN_REQUIRED", "Configure a public HTTPS event origin"
                )
            found = await self._matching_remote(
                token.access_token, credentials.client_id, watch.notification_url,
            )
            if len(found) > 1:
                raise OutlookError(
                    "OUTLOOK_SUBSCRIPTION_AMBIGUOUS",
                    "Multiple matching Graph subscriptions require manual review",
                )
            if not same:
                if found:
                    identifier = found[0].get("id")
                    if not isinstance(identifier, str) or not _GRAPH_ID.fullmatch(identifier):
                        raise OutlookError(
                            "OUTLOOK_SUBSCRIPTION_INVALID", "Graph subscription ID is invalid",
                            502,
                        )
                    try:
                        await self.service._json(
                            "DELETE", GRAPH_API + "/subscriptions/" + identifier,
                            headers={"Authorization": f"Bearer {token.access_token}"},
                        )
                    except OutlookError as exc:
                        if exc.code != "OUTLOOK_NOT_FOUND":
                            raise
                updated = OutlookWatch(
                    connector_id=connector_id, connector_revision=token.connector_revision,
                    tenant_id=tenant_id, user_id=token.user_id, email=token.email,
                    notification_url=new_url, status="removed", gap=True,
                    remote_unverified=not bool(found),
                    delta_from=None,
                    updated_at=datetime.now(UTC),
                )
                self._save(updated)
                self._enroll(trigger, updated)
                assert self.service.connectors.vault is not None
                self.service.connectors.vault.delete(_STATE_KEY + connector_id)
                return self.status(connector_id, tenant_id, trigger)
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
            self.service.connectors.vault.delete(_DELTA_KEY + connector_id)
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

    def _enqueue(
        self, watch: OutlookWatch, message_id: str, targets: list[str],
    ) -> bool:
        if not watch.subscription_id or not targets:
            return False
        if len(targets) > 1000:
            raise OutlookError("OUTLOOK_QUEUE_FULL", "Too many Outlook triggers", 503)
        path = self._queue_path(watch.connector_id, watch.subscription_id, message_id)
        with self.ledger._lock_for(f"outlook-queue:{path.stem}"):
            if path.exists():
                return False
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if len(list(path.parent.glob("*.json"))) >= 10_000:
                raise OutlookError(
                    "OUTLOOK_QUEUE_FULL", "Outlook notification queue is full", 503,
                )
            self.ledger._atomic_write(path, OutlookNotification(
                connector_id=watch.connector_id, subscription_id=watch.subscription_id,
                message_id=message_id, targets=targets, received_at=time.time(),
            ))
            return True

    def _pending_for(self, watch: OutlookWatch) -> bool:
        for path in (self.root / self.ledger._safe_id(watch.connector_id)).glob("*.json"):
            notice = OutlookNotification.model_validate_json(path.read_text("utf-8"))
            if notice.subscription_id == watch.subscription_id and notice.finished_at is None:
                return True
        return False

    async def sync_delta(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger | None = None,
    ) -> dict[str, Any]:
        """Enqueue created Inbox IDs, then advance the private Graph delta cursor."""
        try:
            return await asyncio.wait_for(
                self._sync_delta(connector_id, tenant_id, trigger), timeout=120,
            )
        except (OutlookError, TimeoutError, OSError) as exc:
            if not (
                isinstance(exc, OutlookError) and exc.code in {
                    "OUTLOOK_SUBSCRIPTION_BUSY", "OUTLOOK_DELTA_UNAVAILABLE",
                }
            ):
                with self.ledger._lock_for(f"outlook-watch:{connector_id}"):
                    watch = self._watch(connector_id)
                    if (
                        watch is not None and not watch.gap
                        and self._matches(watch, tenant_id)
                    ):
                        self._save(watch.model_copy(update={
                            "gap": True, "updated_at": datetime.now(UTC),
                        }))
            if isinstance(exc, TimeoutError):
                raise OutlookError(
                    "OUTLOOK_DELTA_TIMEOUT", "Outlook Inbox delta timed out", 502,
                ) from None
            raise

    async def _sync_delta(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger | None,
    ) -> dict[str, Any]:
        token = await self.service._access(connector_id, tenant_id)
        with self._remote_lock(connector_id):
            watch = self._watch(connector_id)
            if (
                watch is None or watch.status not in {"active", "removed"}
                or watch.subscription_id is None
                or not self._matches(watch, tenant_id)
                or token.tenant_id != tenant_id
            ):
                raise OutlookError(
                    "OUTLOOK_DELTA_UNAVAILABLE", "Outlook Inbox delta is unavailable",
                )
            triggers = self._triggers(watch)
            if trigger is not None and not any(
                item.project_id == trigger.project_id and item.id == trigger.id
                for item in triggers
            ):
                raise OutlookError(
                    "OUTLOOK_DELTA_UNAVAILABLE", "Outlook trigger enrollment is unavailable",
                )
            cursor = self._delta_cursor(watch)
            headers = {
                "Authorization": f"Bearer {token.access_token}",
                "Prefer": 'IdType="ImmutableId", odata.maxpagesize=100',
            }
            if cursor is None:
                start = watch.delta_from
                if (
                    start is None or start.tzinfo is None
                    or start > datetime.now(UTC)
                    or start < datetime.now(UTC) - timedelta(days=7)
                ):
                    raise OutlookError(
                        "OUTLOOK_DELTA_UNBOUNDED",
                        "A safe Outlook mail gap boundary is unavailable; inspect the mailbox",
                    )
                folder = await self.service._json(
                    "GET", GRAPH_API + "/me/mailFolders/inbox",
                    headers=headers, params={"$select": "id"},
                )
                folder_id = folder.get("id")
                if not isinstance(folder_id, str) or not _MESSAGE_ID.fullmatch(folder_id):
                    raise OutlookError(
                        "OUTLOOK_DELTA_INVALID", "Graph Inbox folder ID is invalid", 502,
                    )
                url = GRAPH_API + _DELTA_PATH.format(
                    folder_id=quote(folder_id, safe=""),
                ) + "?" + urlencode({
                    "changeType": "created", "$select": "id,receivedDateTime",
                    "$filter": "receivedDateTime ge " + start.astimezone(UTC).isoformat(
                        timespec="seconds",
                    ).replace("+00:00", "Z"),
                })
            else:
                folder_id = cursor.folder_id
                url = cursor.url
                start = None
            targets = []
            for item in triggers:
                enrollment = self._enrollment(item)
                if enrollment is not None and enrollment.enrolled_at is not None:
                    targets.append((f"{item.project_id}:{item.id}", enrollment.enrolled_at))
            if not targets:
                raise OutlookError(
                    "OUTLOOK_DELTA_UNBOUNDED", "No dated Outlook trigger enrollment",
                )
            seen: set[str] = set()
            queued = 0
            total = 0
            for _page in range(_DELTA_PAGE_LIMIT):
                if url in seen:
                    raise OutlookError(
                        "OUTLOOK_DELTA_INVALID", "Graph delta paging loop", 502,
                    )
                seen.add(url)
                page = await self.service._json("GET", url, headers=headers)
                if not self._matches(watch, tenant_id):
                    raise OutlookError(
                        "OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed",
                    )
                items = page.get("value")
                if not isinstance(items, list) or len(items) > 1000:
                    raise OutlookError(
                        "OUTLOOK_DELTA_INVALID", "Graph delta page is invalid", 502,
                    )
                total += len(items)
                if total >= _DELTA_ITEM_LIMIT:
                    raise OutlookError(
                        "OUTLOOK_DELTA_LIMIT",
                        "Outlook gap exceeds the safe delta limit; inspect the mailbox",
                    )
                for item in items:
                    if not isinstance(item, dict):
                        raise OutlookError(
                            "OUTLOOK_DELTA_INVALID", "Graph delta item is invalid", 502,
                        )
                    if "@removed" in item:
                        continue
                    message_id = item.get("id")
                    raw_received = item.get("receivedDateTime")
                    try:
                        if (
                            not isinstance(message_id, str)
                            or not _MESSAGE_ID.fullmatch(message_id)
                            or not isinstance(raw_received, str)
                            or len(raw_received) > 64
                        ):
                            raise ValueError
                        received = datetime.fromisoformat(
                            raw_received.replace("Z", "+00:00"),
                        )
                        if received.tzinfo is None:
                            raise ValueError
                    except ValueError:
                        raise OutlookError(
                            "OUTLOOK_DELTA_INVALID", "Graph delta item is invalid", 502,
                        ) from None
                    if start is not None and received < start:
                        continue
                    eligible = [
                        target for target, enrolled_at in targets
                        if enrolled_at.tzinfo is not None and received >= enrolled_at
                    ]
                    if self._enqueue(watch, message_id, eligible):
                        queued += 1
                next_url = page.get("@odata.nextLink")
                delta_url = page.get("@odata.deltaLink")
                if bool(next_url) == bool(delta_url):
                    raise OutlookError(
                        "OUTLOOK_DELTA_INVALID", "Graph delta continuation is invalid", 502,
                    )
                if delta_url:
                    final_url = self._delta_url(delta_url, folder_id)
                    if "$deltatoken" not in dict(parse_qsl(urlsplit(final_url).query)):
                        raise OutlookError(
                            "OUTLOOK_DELTA_INVALID", "Graph delta link is invalid", 502,
                        )
                    break
                url = self._delta_url(next_url, folder_id)
            else:
                raise OutlookError(
                    "OUTLOOK_DELTA_LIMIT", "Graph delta page limit reached", 502,
                )
            current = self._watch(connector_id)
            if (
                current is None or current.connector_revision != watch.connector_revision
                or current.user_id != watch.user_id
                or current.subscription_id != watch.subscription_id
                or not self._matches(current, tenant_id)
            ):
                raise OutlookError(
                    "OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed",
                )
            assert self.service.connectors.vault is not None
            synced_at = datetime.now(UTC)
            self.service.connectors.vault.set(
                _DELTA_KEY + connector_id,
                OutlookDeltaCursor(
                    connector_revision=watch.connector_revision,
                    tenant_id=watch.tenant_id, user_id=watch.user_id,
                    folder_id=folder_id, url=final_url, synced_at=synced_at,
                ).model_dump_json(),
            )
            # A lifecycle event arriving during this round may refer to
            # changes after Graph issued its deltaLink. Leave that gap set.
            if current.updated_at == watch.updated_at:
                self._save(current.model_copy(update={
                    "gap": False, "updated_at": synced_at,
                }))
            if trigger is None:
                return {"queued": queued, "delta_ready": True}
            return {**self.status(connector_id, tenant_id, trigger), "queued": queued}

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
            if not targets:
                continue
            # Registration may still be waiting for Graph's create response.
            # Verified notifications can be queued against that remote ID, then
            # processed after the watch becomes active.
            queued_watch = watch.model_copy(update={"subscription_id": sub_id})
            self._enqueue(queued_watch, message_id, targets)
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

    async def sync_due(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                watch = self._watch(path.stem)
                if (
                    watch is None or watch.status not in {"active", "removed"}
                    or watch.subscription_id is None
                    or not self._matches(watch, watch.tenant_id)
                    or not self._triggers(watch)
                ):
                    continue
                try:
                    cursor = self._delta_cursor(watch)
                except OutlookError:
                    # sync_delta records the gap before surfacing the invalid cursor.
                    await self.sync_delta(watch.connector_id, watch.tenant_id)
                    continue
                if cursor is None and watch.delta_from is None:
                    with self.ledger._lock_for(f"outlook-watch:{watch.connector_id}"):
                        current = self._watch(watch.connector_id)
                        if current and current.updated_at == watch.updated_at and not current.gap:
                            self._save(current.model_copy(update={
                                "gap": True, "updated_at": datetime.now(UTC),
                            }))
                    continue
                if (
                    cursor is not None and not watch.gap
                    and cursor.synced_at > datetime.now(UTC) - timedelta(minutes=5)
                ):
                    continue
                await self.sync_delta(watch.connector_id, watch.tenant_id)
            except (OutlookError, ValueError, OSError):
                LOGGER.exception("Outlook Inbox delta sync failed for %s", path.stem)

    async def start(self) -> None:
        if self._task is not None:
            return

        async def worker() -> None:
            cycle = 0
            while True:
                try:
                    await self.process_pending()
                    if cycle % 12 == 0:
                        await self.sync_due()
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
