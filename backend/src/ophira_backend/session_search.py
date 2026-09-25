"""Bounded project-scoped search over durable prompts and SDK-visible results."""

from __future__ import annotations

import base64
import binascii
import hashlib
import heapq
from datetime import datetime
from typing import Literal

from pydantic import Field

from .constants import EventType, RunStatus
from .events import _redact_text
from .models import APIModel, RunRecord, utc_now
from .storage import EventReplayBudget, EventReplayLimitError, FileLedger

MAX_QUERY_CHARS = 200
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_RUNS = 500
MAX_RUN_EVENT_BYTES = 512 * 1024
MAX_REQUEST_EVENT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_EVENTS = 10_000
MAX_EXCERPT_CHARS = 320
MAX_CURSOR_CHARS = 256


class SessionSearchQuery(APIModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(default=10, ge=1, le=MAX_SEARCH_RESULTS, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=MAX_CURSOR_CHARS)


class SessionMatch(APIModel):
    run_id: str
    status: RunStatus
    created_at: datetime
    source: Literal["prompt", "user_message", "assistant_message", "result"]
    excerpt: str
    event_sequence: int | None = None


class SessionSearch(APIModel):
    project_id: str
    query: str
    observed_at: datetime
    matches: list[SessionMatch]
    total_runs: int
    scanned_runs: int
    skipped_event_logs: int
    truncated: bool
    next_cursor: str | None


def _excerpt(text: str, needle: str) -> str:
    safe_text = _redact_text(text)
    start = max(0, safe_text.casefold().find(needle) - 90)
    end = min(len(safe_text), start + MAX_EXCERPT_CHARS)
    return ("…" if start else "") + safe_text[start:end] + ("…" if end < len(safe_text) else "")


def _cursor_scope(project_id: str, needle: str) -> str:
    return hashlib.sha256(f"{project_id}\0{needle}".encode()).hexdigest()[:24]


def _encode_cursor(project_id: str, needle: str, created_at: datetime, run_id: str) -> str:
    raw = f"1|{_cursor_scope(project_id, needle)}|{created_at.isoformat()}|{run_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, project_id: str, needle: str) -> tuple[datetime, str]:
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
        ).decode()
        version, scope, timestamp, run_id = raw.split("|", 3)
        created_at = datetime.fromisoformat(timestamp)
        if (version != "1" or scope != _cursor_scope(project_id, needle)
                or created_at.utcoffset() is None):
            raise ValueError("invalid cursor scope")
        FileLedger._safe_id(run_id)
        return created_at, run_id
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise ValueError("invalid session search cursor") from exc


def search_project_sessions(
    ledger: FileLedger, project_id: str, *, query: str, limit: int = 10,
    cursor: str | None = None,
) -> SessionSearch:
    request = SessionSearchQuery(query=query, limit=limit, cursor=cursor)
    needle = request.query.strip().casefold()
    if not needle:
        raise ValueError("query must contain non-whitespace text")
    cursor_key = _decode_cursor(cursor, project_id, needle) if cursor else None
    candidates: list[tuple[datetime, str, RunRecord]] = []
    total_runs = 0
    for run in ledger.iter_runs(project_id):
        total_runs += 1
        key = (run.created_at, run.id)
        if cursor_key is not None and key >= cursor_key:
            continue
        entry = (run.created_at, run.id, run)
        if len(candidates) < MAX_SEARCH_RUNS + 1:
            heapq.heappush(candidates, entry)
        elif key > candidates[0][:2]:
            heapq.heapreplace(candidates, entry)
    eligible = [entry[2] for entry in sorted(
        candidates, key=lambda entry: entry[:2], reverse=True,
    )]
    budget = EventReplayBudget(MAX_REQUEST_EVENT_BYTES, MAX_REQUEST_EVENTS)
    matches: list[SessionMatch] = []
    scanned = 0
    skipped = 0
    last_run = None
    for run in eligible:
        if len(matches) >= request.limit or scanned >= MAX_SEARCH_RUNS:
            break
        if needle in _redact_text(run.prompt).casefold():
            matches.append(SessionMatch(
                run_id=run.id, status=run.status, created_at=run.created_at,
                source="prompt", excerpt=_excerpt(run.prompt, needle),
            ))
        else:
            try:
                events = ledger.list_events(
                    run.id, max_bytes=MAX_RUN_EVENT_BYTES, budget=budget,
                )
            except EventReplayLimitError as exc:
                if exc.reason.startswith("request_") and scanned:
                    # Retry this Run with a fresh request budget on the next page.
                    break
                skipped += 1
                if exc.reason.startswith("request_"):
                    # A single log can exceed a reduced request budget. Advance
                    # with explicit incomplete coverage instead of looping forever.
                    scanned += 1
                    last_run = run
                    break
            else:
                for event in reversed(events):
                    if event.type == EventType.RUN_RESULT.value:
                        text = event.data.get("result")
                        source = "result"
                    elif (event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value
                          and event.actor.type == "agent" and event.parent_id is None):
                        text = event.data.get("text")
                        source = "assistant_message"
                    elif (event.type == EventType.RUN_INTERJECTION_REQUESTED.value
                          and event.actor.type == "user"):
                        text = event.data.get("prompt")
                        source = "user_message"
                    else:
                        continue
                    if isinstance(text, str) and needle in text.casefold():
                        matches.append(SessionMatch(
                            run_id=run.id, status=run.status, created_at=run.created_at,
                            source=source, excerpt=_excerpt(text, needle),
                            event_sequence=event.sequence,
                        ))
                        break
        scanned += 1
        last_run = run
    next_cursor = (
        _encode_cursor(project_id, needle, last_run.created_at, last_run.id)
        if last_run is not None and scanned < len(eligible) else None
    )
    return SessionSearch(
        project_id=project_id, query=request.query, observed_at=utc_now(),
        matches=matches, total_runs=total_runs, scanned_runs=scanned,
        skipped_event_logs=skipped,
        truncated=next_cursor is not None or skipped > 0,
        next_cursor=next_cursor,
    )
