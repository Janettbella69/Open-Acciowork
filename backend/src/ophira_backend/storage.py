from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import (
    EventEnvelope,
    OpportunityProject,
    ProjectCreate,
    RunRecord,
    RunSummary,
    utc_now,
)


class LedgerNotFoundError(KeyError):
    pass


class FileLedger:
    """Durable JSON/JSONL ledger with atomic snapshots and append-only detail logs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.projects_dir = self.root / "projects"
        self.runs_dir = self.root / "runs"
        self.manifest_path = self.root / "_manifest.jsonl"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _safe_id(value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not value or value in {".", ".."} or any(char not in allowed for char in value):
            raise ValueError("invalid ledger identifier")
        return value

    @staticmethod
    def _dump(model: Any) -> str:
        if hasattr(model, "model_dump"):
            value = model.model_dump(mode="json")
        else:
            value = model
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _atomic_write(self, path: Path, model: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(f"{self._dump(model)}\n", encoding="utf-8")
        os.replace(temporary, path)

    def _append_jsonl(self, path: Path, model: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{self._dump(model)}\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _project_path(self, project_id: str) -> Path:
        return self.projects_dir / self._safe_id(project_id) / "project.json"

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / self._safe_id(run_id) / "run.json"

    def _summary_path(self, run_id: str) -> Path:
        return self.runs_dir / self._safe_id(run_id) / "summary.json"

    def _events_path(self, run_id: str) -> Path:
        return self.runs_dir / self._safe_id(run_id) / "events.jsonl"

    def create_project(self, payload: ProjectCreate) -> OpportunityProject:
        project = OpportunityProject(**payload.model_dump())
        with self._lock_for(project.id):
            self._atomic_write(self._project_path(project.id), project)
        return project

    def get_project(self, project_id: str) -> OpportunityProject:
        path = self._project_path(project_id)
        if not path.exists():
            raise LedgerNotFoundError(project_id)
        return OpportunityProject.model_validate_json(path.read_text(encoding="utf-8"))

    def list_projects(self) -> list[OpportunityProject]:
        projects: list[OpportunityProject] = []
        for path in self.projects_dir.glob("*/project.json"):
            try:
                projects.append(
                    OpportunityProject.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.updated_at, reverse=True)

    def update_project(
        self,
        project_id: str,
        mutate: Callable[[OpportunityProject], None],
    ) -> OpportunityProject:
        with self._lock_for(project_id):
            project = self.get_project(project_id)
            mutate(project)
            project.revision += 1
            project.updated_at = utc_now()
            self._atomic_write(self._project_path(project_id), project)
            return project

    def create_run(self, project_id: str, prompt: str) -> RunRecord:
        self.get_project(project_id)
        run = RunRecord(project_id=project_id, prompt=prompt)
        summary = RunSummary(
            run_id=run.id,
            project_id=project_id,
            trace_id=run.trace_id,
            status=run.status,
        )
        with self._lock_for(run.id):
            self._atomic_write(self._run_path(run.id), run)
            self._atomic_write(self._summary_path(run.id), summary)
            self._append_manifest(run, 0)
        return run

    def get_run(self, run_id: str) -> RunRecord:
        path = self._run_path(run_id)
        if not path.exists():
            raise LedgerNotFoundError(run_id)
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def update_run(self, run_id: str, **changes: Any) -> RunRecord:
        with self._lock_for(run_id):
            run = self.get_run(run_id)
            updated = run.model_copy(update=changes)
            self._atomic_write(self._run_path(run_id), updated)
            summary = self.get_run_summary(run_id).model_copy(
                update={"status": updated.status, "updated_at": utc_now()}
            )
            self._atomic_write(self._summary_path(run_id), summary)
            if updated.status != run.status:
                self._append_manifest(updated, summary.last_sequence)
            return updated

    def get_run_summary(self, run_id: str) -> RunSummary:
        path = self._summary_path(run_id)
        if not path.exists():
            raise LedgerNotFoundError(run_id)
        return RunSummary.model_validate_json(path.read_text(encoding="utf-8"))

    def append_event(self, event: EventEnvelope) -> EventEnvelope:
        with self._lock_for(event.run_id):
            summary = self.get_run_summary(event.run_id)
            sequence = summary.last_sequence + 1
            stored = event.model_copy(
                update={"id": f"{event.run_id}:{sequence}", "sequence": sequence}
            )
            self._append_jsonl(self._events_path(event.run_id), stored)
            counts = dict(summary.event_counts)
            counts[stored.type] = counts.get(stored.type, 0) + 1
            summary = summary.model_copy(
                update={
                    "last_sequence": sequence,
                    "event_counts": counts,
                    "updated_at": utc_now(),
                }
            )
            self._atomic_write(self._summary_path(event.run_id), summary)
            if stored.type.startswith("run."):
                self._append_manifest(self.get_run(event.run_id), sequence)
            return stored

    def list_events(self, run_id: str, after_sequence: int = 0) -> list[EventEnvelope]:
        self.get_run(run_id)
        path = self._events_path(run_id)
        if not path.exists():
            return []
        events: list[EventEnvelope] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = EventEnvelope.model_validate_json(line)
            if event.sequence > after_sequence:
                events.append(event)
        return events

    def _append_manifest(self, run: RunRecord, last_sequence: int) -> None:
        self._append_jsonl(
            self.manifest_path,
            {
                "run_id": run.id,
                "project_id": run.project_id,
                "trace_id": run.trace_id,
                "status": run.status,
                "last_sequence": last_sequence,
                "timestamp": utc_now().isoformat(),
            },
        )

    def workspace_for_project(self, project_id: str) -> Path:
        self.get_project(project_id)
        workspace = self.projects_dir / self._safe_id(project_id) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace
