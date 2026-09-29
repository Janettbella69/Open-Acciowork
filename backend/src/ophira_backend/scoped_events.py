"""Durable observations whose authority is a project or Provider, not a Run.

The caller authorizes a project or derives the Provider tenant from its verified
principal. A stream identifier is a scope, never an access token. Events observe
already committed host records; repairing missing observations belongs to those
records' projectors and must never repeat an Agent or external action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from .events import redact
from .models import APIModel, EventActor, new_id, utc_now
from .skill_proposal_files import child_directory, directory_fd
from .storage import FileLedger

MAX_SCOPED_EVENT_BYTES = 128 * 1024
MAX_SCOPED_LOG_BYTES = 64 * 1024 * 1024
MAX_SCOPED_EVENTS = 100_000
MAX_SCOPED_PAGE_SIZE = 200
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}")
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}")
_PROVIDER_SCOPE = re.compile(r"local|tenant-[0-9a-f]{64}")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


class ScopedEventIntegrityError(ValueError):
    """The durable stream is malformed; its original bytes remain untouched."""


class ScopedEventLimitError(ValueError):
    """An explicit event, page or stream bound was reached."""


class ScopedEventCursorError(ValueError):
    """A replay cursor is malformed, ahead of the log or from another scope."""


class ScopedEventEnvelope(APIModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    stream_id: str
    sequence: int = Field(ge=1, strict=True)
    type: str
    resource_id: str
    project_id: str | None = None
    run_id: str | None = None
    provider_profile_id: str | None = None
    trace_id: str
    sdk_session_id: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    actor: EventActor
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Stream:
    id: str
    parts: tuple[str, ...]
    project_id: str | None = None
    provider_id: str | None = None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid scoped event identifier")
    return value


def _parse_stream(value: str) -> _Stream:
    if not isinstance(value, str) or len(value) > 400:
        raise ValueError("Invalid event stream")
    parts = value.split(":")
    if len(parts) == 2 and parts[0] == "project":
        project_id = _identifier(parts[1])
        return _Stream(value, ("projects", project_id, "observations"), project_id=project_id)
    if (len(parts) == 3 and parts[0] == "provider"
            and _PROVIDER_SCOPE.fullmatch(parts[1])):
        provider_id = _identifier(parts[2])
        return _Stream(
            value, ("control-events", "providers", parts[1], provider_id),
            provider_id=provider_id,
        )
    raise ValueError("Invalid event stream")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScopedEventIntegrityError("Duplicate event JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str):
    raise ValueError("Non-finite event JSON number")


class ScopedEventStore:
    """One append-only authority per scope, with no separate deduplication index.

    Full streaming validation makes missing, reordered and duplicate rows fail
    closed even when the requested page precedes the damaged row. Only the page
    and bounded uniqueness keys are retained in memory. Explicit size limits
    reject growth, never silently prune history or reset a sequence.
    """

    def __init__(self, ledger: FileLedger) -> None:
        self.ledger = ledger

    @staticmethod
    def project_stream(project_id: str) -> str:
        return f"project:{_identifier(project_id)}"

    @staticmethod
    def provider_stream(provider_id: str, tenant_id: str | None = None) -> str:
        provider_id = _identifier(provider_id)
        if tenant_id is not None:
            if not isinstance(tenant_id, str) or _TENANT.fullmatch(tenant_id) is None:
                raise ValueError("Invalid Provider event tenant")
            scope = "tenant-" + hashlib.sha256(tenant_id.encode()).hexdigest()
        else:
            scope = "local"
        # No Profile or secret lookup: configuration failures can be observed
        # before any usable Provider profile exists.
        return f"provider:{scope}:{provider_id}"

    @contextmanager
    def _transaction(self, stream: _Stream):
        lease = (self.ledger.lifecycle.lease(stream.project_id)
                 if stream.project_id else nullcontext())
        with lease, self.ledger._lock_for(f"scoped-events:{stream.id}"):
            if stream.project_id:
                self.ledger.get_project(stream.project_id)
            yield

    @contextmanager
    def _directory(self, stream: _Stream, *, create: bool = False) -> Iterator[int]:
        # Directory descriptors protect every ancestor, including against a
        # linked provider scope or a workspace-authored observations symlink.
        with directory_fd(self.ledger.root) as root:
            current = os.dup(root)
            try:
                for index, name in enumerate(stream.parts):
                    # Existing project roots must never be recreated after deletion.
                    may_create = create and (stream.project_id is None or index > 1)
                    if may_create:
                        try:
                            os.mkdir(name, 0o700, dir_fd=current)
                            os.fsync(current)
                        except FileExistsError:
                            pass
                    with child_directory(current, (name,)) as child:
                        next_fd = os.dup(child)
                    os.close(current)
                    current = next_fd
                yield current
            finally:
                os.close(current)

    @staticmethod
    def _validate_event(event: ScopedEventEnvelope, stream: _Stream, sequence: int) -> None:
        if (event.stream_id != stream.id or event.sequence != sequence
                or event.id != f"{stream.id}:{sequence}"
                or event.project_id != stream.project_id
                or not _EVENT_TYPE.fullmatch(event.type)
                or not _RESOURCE.fullmatch(event.resource_id)
                or event.data.get("resource_id") != event.resource_id
                or event.timestamp.tzinfo is None
                or not event.trace_id
                or (stream.provider_id is not None and (
                    event.provider_profile_id != stream.provider_id or event.run_id is not None
                ))):
            raise ScopedEventIntegrityError("Event identity is outside its stream")

    def _scan(
        self, stream: _Stream, *, after: int = 0, limit: int = 0,
        find: tuple[str, str] | None = None,
        keys: set[tuple[str, str]] | None = None,
    ) -> tuple[list[ScopedEventEnvelope], int, int, ScopedEventEnvelope | None]:
        page: list[ScopedEventEnvelope] = []
        count, byte_count = 0, 0
        found = None
        seen: set[tuple[str, str]] = keys if keys is not None else set()
        try:
            with self._directory(stream) as parent:
                descriptor = os.open(
                    "events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=parent,
                )
        except FileNotFoundError:
            return page, count, byte_count, found
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ScopedEventIntegrityError("Event log must be a private regular file")
            if info.st_size > MAX_SCOPED_LOG_BYTES:
                raise ScopedEventLimitError("Scoped event log exceeds its byte limit")
            while raw := handle.readline(MAX_SCOPED_EVENT_BYTES + 1):
                if len(raw) > MAX_SCOPED_EVENT_BYTES:
                    raise ScopedEventLimitError("Scoped event exceeds its byte limit")
                if not raw.endswith(b"\n"):
                    raise ScopedEventIntegrityError("Incomplete event log record")
                count += 1
                byte_count += len(raw)
                if count > MAX_SCOPED_EVENTS or byte_count > MAX_SCOPED_LOG_BYTES:
                    raise ScopedEventLimitError("Scoped event stream exceeds its limit")
                try:
                    event = ScopedEventEnvelope.model_validate(json.loads(
                        raw, object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant,
                    ))
                except (ValueError, UnicodeError, ValidationError) as exc:
                    raise ScopedEventIntegrityError("Invalid event log record") from exc
                self._validate_event(event, stream, count)
                key = (event.type, event.resource_id)
                if key in seen:
                    raise ScopedEventIntegrityError("Duplicate event resource in stream")
                seen.add(key)
                if key == find:
                    found = event
                if event.sequence > after and len(page) < limit:
                    page.append(event)
            final = os.fstat(handle.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                final.st_size, final.st_mtime_ns, final.st_ctime_ns,
            ):
                raise ScopedEventIntegrityError("Event log changed while reading")
        return page, count, byte_count, found

    def append(
        self, stream_id: str, event_type: str | Enum, resource_id: str, data: dict[str, Any],
        *, run_id: str | None = None, trace_id: str | None = None,
        sdk_session_id: str | None = None, actor: EventActor | None = None,
    ) -> ScopedEventEnvelope:
        stream = _parse_stream(stream_id)
        kind = event_type.value if isinstance(event_type, Enum) else event_type
        if not isinstance(kind, str) or _EVENT_TYPE.fullmatch(kind) is None:
            raise ValueError("Invalid scoped event type")
        if not isinstance(resource_id, str) or _RESOURCE.fullmatch(resource_id) is None:
            raise ValueError("Invalid scoped event resource")
        if not isinstance(data, dict) or data.get("resource_id", resource_id) != resource_id:
            raise ValueError("Scoped event resource does not match its payload")
        with self._transaction(stream):
            provider_id = stream.provider_id
            if run_id is not None:
                if stream.project_id is None:
                    raise ValueError("Provider observations cannot claim a project Run")
                run = self.ledger.get_run(_identifier(run_id))
                if (run.project_id != stream.project_id
                        or (trace_id is not None and trace_id != run.trace_id)
                        or (sdk_session_id is not None and sdk_session_id != run.sdk_session_id)):
                    raise ValueError("Observation Run identity does not match its project")
                trace_id, sdk_session_id = run.trace_id, run.sdk_session_id
                provider_id = run.provider_profile_id
            _, count, byte_count, previous = self._scan(stream, find=(kind, resource_id))
            if previous is not None:
                return previous
            event = ScopedEventEnvelope(
                id=f"{stream.id}:{count + 1}", stream_id=stream.id, sequence=count + 1,
                type=kind, resource_id=resource_id, project_id=stream.project_id,
                run_id=run_id, provider_profile_id=provider_id,
                trace_id=trace_id or new_id("trace"), sdk_session_id=sdk_session_id,
                actor=EventActor.model_validate(redact(
                    (actor or EventActor(type="system", name="control-plane")).model_dump()
                )),
                data=redact({**data, "resource_id": resource_id}),
            )
            self._validate_event(event, stream, count + 1)
            raw = (json.dumps(event.model_dump(mode="json"), ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(raw) > MAX_SCOPED_EVENT_BYTES:
                raise ScopedEventLimitError("Scoped event exceeds its byte limit")
            if count >= MAX_SCOPED_EVENTS or byte_count + len(raw) > MAX_SCOPED_LOG_BYTES:
                raise ScopedEventLimitError("Scoped event stream exceeds its limit")
            with self._directory(stream, create=True) as parent:
                descriptor = os.open(
                    "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                    | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent,
                )
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_size != byte_count):
                        raise ScopedEventIntegrityError("Event log changed before append")
                    remaining = memoryview(raw)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("Event append made no progress")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                    os.fsync(parent)
                finally:
                    os.close(descriptor)
            return event

    def read(
        self, stream_id: str, after_sequence: int = 0, limit: int = MAX_SCOPED_PAGE_SIZE,
    ) -> list[ScopedEventEnvelope]:
        stream = _parse_stream(stream_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ScopedEventCursorError("Event cursor must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_SCOPED_PAGE_SIZE:
            raise ScopedEventLimitError("Invalid scoped event page size")
        with self._transaction(stream):
            page, last, _, _ = self._scan(stream, after=after_sequence, limit=limit)
            if after_sequence > last:
                raise ScopedEventCursorError("Event cursor is ahead of this stream")
            return page

    def last_sequence(self, stream_id: str) -> int:
        stream = _parse_stream(stream_id)
        with self._transaction(stream):
            return self._scan(stream)[1]

    def find(
        self, stream_id: str, event_type: str | Enum, resource_id: str,
    ) -> ScopedEventEnvelope | None:
        """Recover one durable decision without skipping stream integrity checks."""
        stream = _parse_stream(stream_id)
        kind = event_type.value if isinstance(event_type, Enum) else event_type
        if not isinstance(kind, str) or _EVENT_TYPE.fullmatch(kind) is None:
            raise ValueError("Invalid scoped event type")
        if not isinstance(resource_id, str) or _RESOURCE.fullmatch(resource_id) is None:
            raise ValueError("Invalid scoped event resource")
        with self._transaction(stream):
            return self._scan(stream, find=(kind, resource_id))[3]

    def observed_keys(self, stream_id: str) -> set[tuple[str, str]]:
        """Read a validated key snapshot in one pass for batch reconciliation.

        New events may arrive after this snapshot. append still performs the
        authoritative locked deduplication; this set is only a work-saving hint.
        """
        stream = _parse_stream(stream_id)
        keys: set[tuple[str, str]] = set()
        with self._transaction(stream):
            self._scan(stream, keys=keys)
        return keys

    def parse_cursor(self, stream_id: str, cursor: str | None) -> int:
        """Accept only a scope-bound SSE ID, preventing cross-stream reuse."""
        _parse_stream(stream_id)
        if cursor is None or cursor == "":
            return 0
        if not isinstance(cursor, str) or len(cursor) > 420:
            raise ScopedEventCursorError("Invalid scoped event cursor")
        prefix, separator, number = cursor.rpartition(":")
        if (not separator or prefix != stream_id or not re.fullmatch(r"0|[1-9][0-9]*", number)
                or len(number) > 12):
            raise ScopedEventCursorError("Event cursor belongs to another stream or is invalid")
        sequence = int(number)
        if sequence > self.last_sequence(stream_id):
            raise ScopedEventCursorError("Event cursor is ahead of this stream")
        return sequence
