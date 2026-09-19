"""Proactive-service stores (PRD §4.16 / FR-15 / FR-26).

NotificationStore is the central inbox: heartbeat results with changes, schedule
failures, and standing-intent hits land here; "nothing happened" stays silent.
IntentStore holds user-authorized standing intents and enforces the anti-nag
budget (cooldown / max triggers / expiry) before any trigger reaches the inbox.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .constants import StandingIntentWatcherStatus
from .models import (
    Notification,
    PrincipalBinding,
    PrincipalContext,
    StandingIntent,
    StandingIntentCreate,
    new_id,
    utc_now,
)
from .project_lifecycle import ProjectLifecycle
from .skill_proposal_files import read_relative


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
        with (ProjectLifecycle(self.root.parent).lease(item.project_id)
              if item.project_id else nullcontext()):
            if item.project_id and getattr(item, "run_id", None):
                ProjectLifecycle(self.root.parent).assert_run_available(
                    item.project_id, item.run_id,
                )
            path = self._path(item.id)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(item.model_dump_json(indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)


class NotificationStore(_FileStore):
    def _write(self, item: Notification) -> None:
        # Older inbox rows can carry a run/schedule/intent without project_id.
        # Resolve these before taking the lease so a stale row cannot reappear
        # after its linked project's content has been erased.
        project_id = item.project_id
        if project_id is None:
            scopes = set()
            for identifier, pattern in (
                (item.run_id, "runs/{}/run.json"),
                (item.schedule_id, "schedules/{}/schedule.json"),
                (item.intent_id, "intents/{}.json"),
            ):
                if identifier is None:
                    continue
                try:
                    raw = read_relative(
                        self.root.parent, pattern.format(_safe_id(identifier)), 16 * 1024 * 1024,
                    )
                except FileNotFoundError as exc:
                    raise NotificationNotFoundError("Notification source is unavailable") from exc
                source = json.loads(raw)
                if source.get("id") != identifier or not source.get("project_id"):
                    raise ValueError("Notification source scope is invalid")
                scopes.add(source["project_id"])
            if len(scopes) > 1:
                raise ValueError("Notification sources have conflicting project scopes")
            if scopes:
                project_id = scopes.pop()
                item = item.model_copy(update={"project_id": project_id})
        super()._write(item)

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

    def list(
        self, *, unread_only: bool = False, limit: int = 200,
        project_ids: set[str] | None = None,
    ) -> list[Notification]:
        items: list[Notification] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(Notification.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item.created_at, reverse=True)
        if unread_only:
            items = [item for item in items if not item.read]
        if project_ids is not None:
            items = [item for item in items if item.project_id in project_ids]
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
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._process_lock_path = self.root / ".intent-store.lock"

    @contextmanager
    def _exclusive(self):
        """Serialize read/modify/write cycles across threads and API workers."""

        with self._lock:
            descriptor = os.open(self._process_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _validated_copy(intent: StandingIntent, **changes) -> StandingIntent:
        values = intent.model_dump()
        values.update(changes)
        return StandingIntent.model_validate(values)

    def create(
        self,
        payload: StandingIntentCreate,
        *,
        now: datetime | None = None,
        authorization_source: Literal["user_api", "user_approved_agent"] = "user_api",
        principal: PrincipalContext | PrincipalBinding | None = None,
    ) -> StandingIntent:
        moment = (now or utc_now()).astimezone(UTC)
        principal_binding = (
            principal
            if isinstance(principal, PrincipalBinding)
            else PrincipalBinding.from_context(principal)
            if principal is not None
            else None
        )
        intent = StandingIntent(
            project_id=payload.project_id,
            principal_binding=principal_binding,
            agent_id=payload.agent_id,
            provider_profile_id=payload.provider_profile_id,
            intent=payload.intent,
            cooldown_hours=payload.cooldown_hours,
            max_triggers=payload.max_triggers,
            expires_at=moment + timedelta(days=payload.expires_days),
            check_interval_minutes=payload.check_interval_minutes,
            check_timeout_seconds=payload.check_timeout_seconds,
            per_check_budget_usd=payload.per_check_budget_usd,
            total_budget_usd=payload.total_budget_usd,
            next_check_at=moment,
            authorization_source=authorization_source,
            authorized_at=moment,
            created_by=principal.principal_id if principal is not None else "user",
            created_at=moment,
        )
        with self._exclusive():
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

    def set_enabled(
        self,
        intent_id: str,
        enabled: bool,
        *,
        now: datetime | None = None,
        authorization_source: Literal["user_api", "user_approved_agent"] | None = None,
    ) -> StandingIntent:
        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if enabled:
                if (
                    intent.authorization_source is None
                    and authorization_source is None
                ):
                    raise ValueError(
                        "legacy standing intent requires explicit user reauthorization"
                    )
                if moment >= intent.expires_at:
                    raise ValueError("expired standing intent cannot be resumed")
                if intent.triggered_count >= intent.max_triggers:
                    raise ValueError("standing intent trigger budget is exhausted")
                remaining = intent.total_budget_usd - (
                    intent.budget_committed_usd + intent.budget_reserved_usd
                )
                if remaining + 1e-9 < intent.per_check_budget_usd:
                    raise ValueError("standing intent check budget is exhausted")
                updated = self._validated_copy(
                    intent,
                    enabled=True,
                    next_check_at=(intent.next_check_at if intent.active_claim_id else moment),
                    watcher_status=(
                        StandingIntentWatcherStatus.CHECKING
                        if intent.active_claim_id
                        else StandingIntentWatcherStatus.IDLE
                    ),
                    authorization_source=(
                        authorization_source or intent.authorization_source
                    ),
                    authorized_at=(
                        moment
                        if authorization_source is not None
                        else intent.authorized_at
                    ),
                )
            else:
                updated = self._validated_copy(
                    intent,
                    enabled=False,
                    next_check_at=None,
                    watcher_status=(
                        StandingIntentWatcherStatus.CHECKING
                        if intent.active_claim_id
                        else StandingIntentWatcherStatus.PAUSED
                    ),
                )
            self._write(updated)
        return updated

    def try_claim_check(
        self,
        intent_id: str,
        *,
        owner: str,
        now: datetime | None = None,
        force: bool = False,
    ) -> tuple[str, StandingIntent]:
        """Atomically reserve one due evaluation without asserting its condition.

        The returned ``claimed`` state authorizes only a bounded Agent evaluation
        run. The watcher itself never calls :meth:`try_trigger`.
        """

        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if intent.active_claim_id is not None:
                if (
                    intent.check_lease_expires_at is not None
                    and intent.check_lease_expires_at <= moment
                ):
                    return "claim_expired", intent
                return "busy", intent
            if not intent.enabled:
                return "disabled", intent
            if intent.authorization_source is None or intent.authorized_at is None:
                updated = self._validated_copy(
                    intent,
                    next_check_at=None,
                    watcher_status=StandingIntentWatcherStatus.UNAUTHORIZED,
                )
                self._write(updated)
                return "unauthorized", updated
            if moment >= intent.expires_at:
                updated = self._validated_copy(
                    intent,
                    enabled=False,
                    next_check_at=None,
                    watcher_status=StandingIntentWatcherStatus.EXPIRED,
                )
                self._write(updated)
                return "expired", updated
            if intent.triggered_count >= intent.max_triggers:
                updated = self._validated_copy(
                    intent,
                    next_check_at=None,
                    watcher_status=StandingIntentWatcherStatus.TRIGGER_EXHAUSTED,
                )
                self._write(updated)
                return "trigger_exhausted", updated
            if intent.last_triggered_at is not None:
                cooldown_until = intent.last_triggered_at + timedelta(
                    hours=intent.cooldown_hours
                )
                if moment < cooldown_until:
                    updated = self._validated_copy(
                        intent,
                        next_check_at=(
                            cooldown_until
                            if intent.next_check_at is None
                            else max(intent.next_check_at, cooldown_until)
                        ),
                        watcher_status=StandingIntentWatcherStatus.IDLE,
                    )
                    self._write(updated)
                    return "cooldown", updated
            remaining = intent.total_budget_usd - (
                intent.budget_committed_usd + intent.budget_reserved_usd
            )
            if remaining + 1e-9 < intent.per_check_budget_usd:
                updated = self._validated_copy(
                    intent,
                    next_check_at=None,
                    watcher_status=StandingIntentWatcherStatus.BUDGET_EXHAUSTED,
                )
                self._write(updated)
                return "budget_exhausted", updated
            if not force and intent.next_check_at is not None and intent.next_check_at > moment:
                return "not_due", intent

            claim_id = new_id("intentclaim")
            updated = self._validated_copy(
                intent,
                active_claim_id=claim_id,
                active_claim_owner=owner,
                check_lease_expires_at=moment
                + timedelta(seconds=intent.check_timeout_seconds),
                budget_reserved_usd=intent.per_check_budget_usd,
                last_check_at=moment,
                next_check_at=moment + timedelta(minutes=intent.check_interval_minutes),
                check_count=intent.check_count + 1,
                watcher_status=StandingIntentWatcherStatus.CHECKING,
                last_check_error=None,
            )
            self._write(updated)
            return "claimed", updated

    def attach_check_run(
        self,
        intent_id: str,
        claim_id: str,
        run_id: str,
        *,
        provider_profile_id: str | None = None,
    ) -> StandingIntent:
        """Bind a durable run and convert its budget reservation to commitment."""

        with self._exclusive():
            intent = self.get(intent_id)
            if intent.active_claim_id != claim_id:
                raise ValueError("standing intent claim is no longer active")
            if intent.active_check_run_id not in {None, run_id}:
                raise ValueError("standing intent claim is already bound to another run")
            committed = intent.budget_committed_usd
            if intent.active_check_run_id is None:
                committed += intent.budget_reserved_usd
            updated = self._validated_copy(
                intent,
                active_check_run_id=run_id,
                last_check_run_id=run_id,
                provider_profile_id=(
                    intent.provider_profile_id or provider_profile_id
                ),
                budget_committed_usd=round(committed, 8),
                budget_reserved_usd=0,
            )
            self._write(updated)
            return updated

    def finish_check(
        self,
        intent_id: str,
        claim_id: str,
        *,
        now: datetime | None = None,
    ) -> StandingIntent:
        """Release a completed evaluation; notification is exclusively tool-driven."""

        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if intent.active_claim_id != claim_id:
                return intent
            committed = intent.budget_committed_usd + intent.budget_reserved_usd
            status = StandingIntentWatcherStatus.IDLE
            next_check_at: datetime | None = moment + timedelta(
                minutes=intent.check_interval_minutes
            )
            enabled = intent.enabled
            if moment >= intent.expires_at:
                enabled = False
                status = StandingIntentWatcherStatus.EXPIRED
                next_check_at = None
            elif intent.triggered_count >= intent.max_triggers:
                status = StandingIntentWatcherStatus.TRIGGER_EXHAUSTED
                next_check_at = None
            elif (
                intent.total_budget_usd - committed + 1e-9
                < intent.per_check_budget_usd
            ):
                status = StandingIntentWatcherStatus.BUDGET_EXHAUSTED
                next_check_at = None
            elif not enabled:
                status = StandingIntentWatcherStatus.PAUSED
                next_check_at = None
            elif intent.last_triggered_at is not None:
                next_check_at = max(
                    next_check_at,
                    intent.last_triggered_at + timedelta(hours=intent.cooldown_hours),
                )
            updated = self._validated_copy(
                intent,
                enabled=enabled,
                active_check_run_id=None,
                active_claim_id=None,
                active_claim_owner=None,
                check_lease_expires_at=None,
                budget_committed_usd=round(committed, 8),
                budget_reserved_usd=0,
                consecutive_failures=0,
                last_check_error=None,
                next_check_at=next_check_at,
                watcher_status=status,
            )
            self._write(updated)
            return updated

    def fail_check(
        self,
        intent_id: str,
        claim_id: str,
        error: str,
        *,
        now: datetime | None = None,
        backoff_seconds: float,
        run_started: bool,
    ) -> StandingIntent:
        """Persist a visible failure and exponentially delayed next attempt."""

        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if intent.active_claim_id != claim_id:
                return intent
            committed = intent.budget_committed_usd
            if run_started:
                committed += intent.budget_reserved_usd
            status = StandingIntentWatcherStatus.BACKOFF
            next_check_at: datetime | None = moment + timedelta(seconds=backoff_seconds)
            enabled = intent.enabled
            if moment >= intent.expires_at:
                enabled = False
                status = StandingIntentWatcherStatus.EXPIRED
                next_check_at = None
            elif (
                intent.total_budget_usd - committed + 1e-9
                < intent.per_check_budget_usd
            ):
                status = StandingIntentWatcherStatus.BUDGET_EXHAUSTED
                next_check_at = None
            elif not enabled:
                status = StandingIntentWatcherStatus.PAUSED
                next_check_at = None
            updated = self._validated_copy(
                intent,
                enabled=enabled,
                active_check_run_id=None,
                active_claim_id=None,
                active_claim_owner=None,
                check_lease_expires_at=None,
                budget_committed_usd=round(committed, 8),
                budget_reserved_usd=0,
                consecutive_failures=intent.consecutive_failures + 1,
                last_check_error=error[:1_000],
                last_check_error_at=moment,
                next_check_at=next_check_at,
                watcher_status=status,
            )
            self._write(updated)
            return updated

    def cancel_check(
        self,
        intent_id: str,
        claim_id: str,
        *,
        now: datetime | None = None,
        run_started: bool,
    ) -> StandingIntent:
        """Release a user-paused check without turning that choice into a failure."""

        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if intent.active_claim_id != claim_id:
                return intent
            committed = intent.budget_committed_usd
            if run_started:
                committed += intent.budget_reserved_usd
            enabled = intent.enabled
            status = (
                StandingIntentWatcherStatus.IDLE
                if enabled
                else StandingIntentWatcherStatus.PAUSED
            )
            next_check_at = (
                moment + timedelta(minutes=intent.check_interval_minutes)
                if enabled
                else None
            )
            if moment >= intent.expires_at:
                enabled = False
                status = StandingIntentWatcherStatus.EXPIRED
                next_check_at = None
            elif intent.triggered_count >= intent.max_triggers:
                status = StandingIntentWatcherStatus.TRIGGER_EXHAUSTED
                next_check_at = None
            elif (
                intent.total_budget_usd - committed + 1e-9
                < intent.per_check_budget_usd
            ):
                status = StandingIntentWatcherStatus.BUDGET_EXHAUSTED
                next_check_at = None
            updated = self._validated_copy(
                intent,
                enabled=enabled,
                active_check_run_id=None,
                active_claim_id=None,
                active_claim_owner=None,
                check_lease_expires_at=None,
                budget_committed_usd=round(committed, 8),
                budget_reserved_usd=0,
                next_check_at=next_check_at,
                watcher_status=status,
            )
            self._write(updated)
            return updated

    def try_trigger(
        self,
        intent_id: str,
        *,
        now: datetime | None = None,
        expected_check_run_id: str | None = None,
    ) -> tuple[str, StandingIntent]:
        """Apply the anti-nag budget in order: disabled → expired → exhausted →
        cooldown → triggered. Only "triggered" may reach the inbox.

        A watcher-scoped caller supplies ``expected_check_run_id``. This prevents a
        timed-out or superseded Agent run from consuming a later check's trigger
        budget after its durable claim has been released.
        """
        moment = (now or utc_now()).astimezone(UTC)
        with self._exclusive():
            intent = self.get(intent_id)
            if expected_check_run_id is not None and (
                intent.active_claim_id is None
                or intent.active_check_run_id != expected_check_run_id
            ):
                return "stale_check", intent
            if not intent.enabled:
                return "disabled", intent
            if moment >= intent.expires_at:
                disabled = self._validated_copy(
                    intent,
                    enabled=False,
                    next_check_at=None,
                    watcher_status=StandingIntentWatcherStatus.EXPIRED,
                )
                self._write(disabled)
                return "expired", disabled
            if intent.triggered_count >= intent.max_triggers:
                return "exhausted", intent
            if intent.last_triggered_at is not None:
                elapsed = moment - intent.last_triggered_at
                if elapsed < timedelta(hours=intent.cooldown_hours):
                    return "cooldown", intent
            count = intent.triggered_count + 1
            next_check_at = (
                None
                if count >= intent.max_triggers
                else moment + timedelta(hours=intent.cooldown_hours)
            )
            triggered = self._validated_copy(
                intent,
                triggered_count=count,
                last_triggered_at=moment,
                next_check_at=next_check_at,
                watcher_status=(
                    StandingIntentWatcherStatus.TRIGGER_EXHAUSTED
                    if count >= intent.max_triggers
                    else intent.watcher_status
                ),
            )
            self._write(triggered)
            return "triggered", triggered
