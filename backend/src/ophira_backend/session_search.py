"""Bounded project-scoped search over durable prompts and SDK-visible results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .constants import EventType, RunStatus
from .events import _redact_text
from .models import APIModel, utc_now
from .storage import EventReplayBudget, EventReplayLimitError, FileLedger

MAX_QUERY_CHARS = 200
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_RUNS = 500
MAX_RUN_EVENT_BYTES = 512 * 1024
MAX_REQUEST_EVENT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_EVENTS = 10_000
MAX_EXCERPT_CHARS = 320


class SessionSearchQuery(APIModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(default=10, ge=1, le=MAX_SEARCH_RESULTS, strict=True)


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


def _excerpt(text: str, needle: str) -> str:
    safe_text = _redact_text(text)
    start = max(0, safe_text.casefold().find(needle) - 90)
    end = min(len(safe_text), start + MAX_EXCERPT_CHARS)
    return ("…" if start else "") + safe_text[start:end] + ("…" if end < len(safe_text) else "")


def search_project_sessions(
    ledger: FileLedger, project_id: str, *, query: str, limit: int = 10,
) -> SessionSearch:
    request = SessionSearchQuery(query=query, limit=limit)
    needle = request.query.strip().casefold()
    if not needle:
        raise ValueError("query must contain non-whitespace text")
    runs = ledger.list_runs(project_id)
    budget = EventReplayBudget(MAX_REQUEST_EVENT_BYTES, MAX_REQUEST_EVENTS)
    matches: list[SessionMatch] = []
    scanned = 0
    skipped = 0
    for run in runs[:MAX_SEARCH_RUNS]:
        if len(matches) >= request.limit:
            break
        scanned += 1
        if needle in _redact_text(run.prompt).casefold():
            matches.append(SessionMatch(
                run_id=run.id, status=run.status, created_at=run.created_at,
                source="prompt", excerpt=_excerpt(run.prompt, needle),
            ))
            continue
        try:
            events = ledger.list_events(
                run.id, max_bytes=MAX_RUN_EVENT_BYTES, budget=budget,
            )
        except EventReplayLimitError as exc:
            skipped += 1
            if exc.reason.startswith("request_"):
                break
            continue
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
    return SessionSearch(
        project_id=project_id, query=request.query, observed_at=utc_now(),
        matches=matches, total_runs=len(runs), scanned_runs=scanned,
        skipped_event_logs=skipped,
        truncated=scanned < len(runs) or skipped > 0,
    )
