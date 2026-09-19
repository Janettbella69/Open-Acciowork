"""User feedback loop (P2 spec): conclusions, evidence issues, failed runs."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .constants import EventType, FeedbackKind, RunStatus
from .events import redact
from .models import (
    APIModel,
    EventEnvelope,
    FeedbackContext,
    FeedbackCreate,
    FeedbackItem,
    FeedbackReview,
    FeedbackReviewRecord,
    FeedbackStatus,
    PrincipalContext,
    RunRecord,
)
from .project_lifecycle import ProjectLifecycle
from .storage import FileLedger

logger = logging.getLogger(__name__)


class FeedbackNotFoundError(KeyError):
    pass


class FeedbackConflictError(ValueError):
    pass


def _redacted_fields(record: APIModel) -> dict:
    """Redaction can expand text; retain the schema bounds with an explicit marker."""
    values = redact(record.model_dump(mode="json"))
    for name, field in type(record).model_fields.items():
        value = values.get(name)
        if not isinstance(value, str):
            continue
        limits = [limit for meta in field.metadata
                  if isinstance(limit := getattr(meta, "max_length", None), int)]
        if limits and len(value) > (limit := min(limits)):
            marker = "…[TRUNCATED]"
            values[name] = value[:max(0, limit - len(marker))] + marker[:limit]
    return values


class FeedbackStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid feedback id")
        return value

    def _path(self, item_id: str) -> Path:
        return self.root / f"{self._safe_id(item_id)}.json"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock:
            descriptor = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _write(self, item: FeedbackItem) -> None:
        with (ProjectLifecycle(self.root.parent).lease(item.project_id)
              if item.project_id else nullcontext()):
            if item.project_id and item.run_id:
                ProjectLifecycle(self.root.parent).assert_run_available(
                    item.project_id, item.run_id,
                )
            descriptor, temporary = tempfile.mkstemp(
                prefix=".feedback-", suffix=".tmp", dir=self.root,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(item.model_dump_json(indent=2) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path(item.id))
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def anchor(
        self, payload: FeedbackCreate, ledger: FileLedger
    ) -> tuple[FeedbackCreate, FeedbackContext]:
        """Resolve all anchors from durable records, never from caller snapshots."""
        run = ledger.get_run(payload.run_id) if payload.run_id else None
        project_id = payload.project_id or (run.project_id if run else None)
        if project_id is None:
            raise ValueError("feedback requires a project")
        project = ledger.get_project(project_id)
        evidence = None
        if payload.evidence_id:
            evidence = next(
                (item for item in project.evidence if item.id == payload.evidence_id), None
            )
            if evidence is None:
                raise FeedbackNotFoundError("Evidence not found")
            if evidence.source_run_id:
                if run is not None and run.id != evidence.source_run_id:
                    raise ValueError("Evidence belongs to a different source run")
                run = ledger.get_run(evidence.source_run_id)
        if run is None:
            raise ValueError(
                "historical evidence requires an explicit project run for trace context"
            )
        if run.project_id != project_id:
            raise ValueError("Run is outside feedback project")
        context = FeedbackContext(trace_id=run.trace_id, run_status=run.status)
        if payload.message_id:
            event = next(
                (item for item in ledger.list_events(run.id) if item.id == payload.message_id), None
            )
            if event is None:
                raise FeedbackNotFoundError("Assistant message not found")
            text = event.data.get("text")
            if (
                event.type != EventType.ASSISTANT_MESSAGE_COMPLETED.value
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError("message anchor must be a completed visible assistant message")
            context.event_id = event.id
            context.event_type = event.type
            context.message_text = str(redact(text))[:20_000]
        if evidence is not None:
            # Preserve what the user reported even if later marked stale/removed.
            context.evidence = evidence.model_validate(_redacted_fields(evidence))
        if payload.kind == FeedbackKind.RUN_FAILURE:
            if run.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
                raise ValueError("failure feedback requires a failed or cancelled run")
            context = self._terminal_context(run, ledger.list_events(run.id))
        return payload.model_copy(update={"project_id": project_id, "run_id": run.id}), context

    def add(
        self,
        payload: FeedbackCreate,
        *,
        ledger: FileLedger,
        principal: PrincipalContext | None = None,
    ) -> FeedbackItem:
        payload, context = self.anchor(payload, ledger)
        identity = (
            {
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "principal_role": principal.role,
                "principal_session_id": principal.session_id,
                "identity_binding": "authenticated_principal",
            }
            if principal is not None
            else {}
        )
        item = FeedbackItem(
            **_redacted_fields(payload),
            context=context,
            **identity,
        )
        with self._exclusive():
            self._write(item)
        return item

    def get(self, feedback_id: str) -> FeedbackItem:
        path = self._path(feedback_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise FeedbackNotFoundError(feedback_id) from exc
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            item = FeedbackItem.model_validate_json(handle.read())
        if item.id != feedback_id:
            raise ValueError("Feedback record does not match its locator")
        return item

    def list(self, *, limit: int | None = 200) -> list[FeedbackItem]:
        items: list[FeedbackItem] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(self.get(path.stem))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit] if limit is not None else items

    def review(
        self,
        feedback_id: str,
        payload: FeedbackReview,
        *,
        principal: PrincipalContext | None = None,
    ) -> FeedbackItem:
        allowed: dict[FeedbackStatus, set[FeedbackStatus]] = {
            "open": {"reviewed", "rejected"},
            "reviewed": {"folded_into_skill", "rejected", "open"},
            "folded_into_skill": {"open"},
            "rejected": {"open"},
        }
        with self._exclusive():
            item = self.get(feedback_id)
            if item.revision != payload.expected_revision:
                raise FeedbackConflictError("Feedback changed; reload before reviewing")
            if payload.status not in allowed[item.status]:
                raise FeedbackConflictError("Invalid feedback review transition")
            if item.context is None or not item.run_id or not item.project_id:
                raise FeedbackConflictError(
                    "Legacy feedback has no trace anchor; submit an anchored report first"
                )
            review = FeedbackReviewRecord(
                from_status=item.status,
                to_status=payload.status,
                note=str(redact(payload.note)),
                reviewer_tenant_id=principal.tenant_id if principal else None,
                reviewer_principal_id=principal.principal_id if principal else None,
                reviewer_role=principal.role if principal else None,
                **redact(
                    payload.model_dump(
                        include={
                            "skill_id",
                            "skill_content_hash",
                            "regression_report",
                            "manual_skill_update_confirmed",
                            "l2_regression_passed_confirmed",
                        }
                    )
                ),
            )
            updated = item.model_copy(
                update={
                    "status": payload.status,
                    "revision": item.revision + 1,
                    "reviews": [*item.reviews, review],
                }
            )
            self._write(updated)
            return updated

    @staticmethod
    def _terminal_context(run: RunRecord, events: Sequence[EventEnvelope]) -> FeedbackContext:
        terminal_type = (
            EventType.RUN_FAILED if run.status == RunStatus.FAILED else EventType.RUN_CANCELLED
        )
        event = next((item for item in reversed(events) if item.type == terminal_type.value), None)
        return FeedbackContext(
            trace_id=run.trace_id,
            run_status=run.status,
            event_id=event.id if event else None,
            event_type=event.type if event else None,
            message_text=str(redact(run.error))[:20_000] if run.error else None,
        )

    def capture_terminal(
        self, run: RunRecord, events: Sequence[EventEnvelope]
    ) -> FeedbackItem | None:
        """Idempotent post-run collection; never decides a verdict or changes a run."""
        if run.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
            return None
        item_id = "feedback_terminal_" + hashlib.sha256(run.id.encode()).hexdigest()[:32]
        context = self._terminal_context(run, events)
        with self._exclusive():
            try:
                existing = self.get(item_id)
            except FeedbackNotFoundError:
                existing = None
            if existing is not None:
                # Repair capture-before-terminal-event without losing human reviews.
                if existing.context and existing.context.event_id is None and context.event_id:
                    existing = existing.model_copy(
                        update={"context": context, "revision": existing.revision + 1}
                    )
                    self._write(existing)
                return existing
            user_cancelled = any(
                event.type == EventType.RUN_CANCEL_REQUESTED.value and event.actor.type == "user"
                for event in events
            )
            note = (
                "Run failed; the cause and quality verdict require human review."
                if run.status == RunStatus.FAILED
                else "User cancelled this run; the reason is not labeled."
                if user_cancelled
                else "Run cancelled during lifecycle cleanup; the reason is not labeled."
            )
            identity = run.model_dump(
                include={
                    "tenant_id",
                    "principal_id",
                    "principal_role",
                    "principal_session_id",
                    "identity_binding",
                }
            )
            item = FeedbackItem(
                id=item_id,
                project_id=run.project_id,
                run_id=run.id,
                kind=FeedbackKind.RUN_FAILURE,
                user_note=note,
                source="system",
                context=context,
                **identity,
            )
            self._write(item)
            return item

    def reconcile_terminal(self, ledger: FileLedger) -> int:
        """Recover the terminal-run / feedback-write crash window on startup."""
        captured = 0
        for run in ledger.list_runs():
            if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                try:
                    self.capture_terminal(run, ledger.list_events(run.id))
                    captured += 1
                except Exception:
                    # Feedback storage is not part of a run's decision or
                    # availability path. Do not hide later recoverable rows.
                    logger.error("feedback reconciliation failed for run %s", run.id)
        return captured
