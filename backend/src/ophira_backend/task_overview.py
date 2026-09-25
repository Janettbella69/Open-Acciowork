"""Bounded, authorization-filtered task list derived from durable Runs."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

from .constants import EventType, RunStatus
from .events import _redact_text
from .models import APIModel, PendingPermissionView, utc_now
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .storage import EventReplayBudget, EventReplayLimitError, FileLedger, LedgerNotFoundError

MAX_TASK_ROWS = 50
MAX_RUN_EVENT_BYTES = 512 * 1024
MAX_REQUEST_EVENT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_EVENTS = 10_000


class TaskRow(APIModel):
    run_id: str
    project_id: str
    project_name: str
    prompt: str
    status: RunStatus
    created_at: datetime
    pending_approvals: int
    max_budget_usd: float | None
    sdk_budget_usd: float | None
    estimated_cost_usd: float | None
    result_preview: str | None
    observations_available: bool


class TaskOverview(APIModel):
    observed_at: datetime
    rows: list[TaskRow]
    total_visible_runs: int
    truncated: bool


def read_task_overview(
    ledger: FileLedger,
    *,
    can_read_run: Callable[[str], bool],
    pending: list[PendingPermissionView],
) -> TaskOverview:
    visible = [run for run in ledger.list_runs() if can_read_run(run.project_id)]
    approvals: dict[str, int] = {}
    for item in pending:
        if item.state in {"pending", "preparing"}:
            approvals[item.run_id] = approvals.get(item.run_id, 0) + 1
    projects: dict[str, str] = {}
    rows: list[TaskRow] = []
    budget = EventReplayBudget(MAX_REQUEST_EVENT_BYTES, MAX_REQUEST_EVENTS)
    truncated = len(visible) > MAX_TASK_ROWS
    for run in visible[:MAX_TASK_ROWS]:
        if run.project_id not in projects:
            try:
                projects[run.project_id] = _redact_text(ledger.get_project(run.project_id).name)
            except (LedgerNotFoundError, ProjectUnavailableError, ProjectLifecycleBusyError):
                truncated = True
                continue
        cost = None
        preview = None
        available = True
        try:
            events = ledger.list_events(run.id, max_bytes=MAX_RUN_EVENT_BYTES, budget=budget)
            for event in reversed(events):
                if event.type != EventType.RUN_RESULT.value:
                    continue
                amount = event.data.get("total_cost_usd")
                if isinstance(amount, int | float) and not isinstance(amount, bool):
                    if math.isfinite(amount) and amount >= 0:
                        cost = float(amount)
                result = event.data.get("result")
                if isinstance(result, str) and result.strip():
                    preview = _redact_text(result)[:300]
                break
        except EventReplayLimitError:
            available = False
            truncated = True
        rows.append(TaskRow(
            run_id=run.id, project_id=run.project_id, project_name=projects[run.project_id],
            prompt=_redact_text(run.prompt)[:300], status=run.status, created_at=run.created_at,
            pending_approvals=approvals.get(run.id, 0), max_budget_usd=run.max_budget_usd,
            sdk_budget_usd=run.sdk_budget_usd,
            estimated_cost_usd=cost, result_preview=preview,
            observations_available=available,
        ))
    return TaskOverview(
        observed_at=utc_now(), rows=rows, total_visible_runs=len(visible),
        truncated=truncated,
    )
