from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import DecisionStatus, EvidenceKind, EvidenceStatus, RunMode
from .models import (
    AgentCreate,
    AgentProfile,
    Artifact,
    EventEnvelope,
    Evidence,
    OpportunityProject,
    ProjectCreate,
    RunRecord,
    RunSummary,
    Schedule,
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
        self.agents_dir = self.root / "agents"
        self.schedules_dir = self.root / "schedules"
        self.manifest_path = self.root / "_manifest.jsonl"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
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

    def create_run(
        self,
        project_id: str,
        prompt: str,
        *,
        agent_id: str | None = None,
        schedule_id: str | None = None,
        resume_session_id: str | None = None,
        mode: RunMode = RunMode.STANDARD,
        provider_profile_id: str | None = None,
        provider_snapshot: dict[str, Any] | None = None,
        skill_snapshot: list[dict[str, Any]] | None = None,
    ) -> RunRecord:
        self.get_project(project_id)
        run = RunRecord(
            project_id=project_id,
            prompt=prompt,
            agent_id=agent_id,
            schedule_id=schedule_id,
            resume_session_id=resume_session_id,
            mode=mode,
            provider_profile_id=provider_profile_id,
            provider_snapshot=provider_snapshot,
            skill_snapshot=skill_snapshot or [],
        )
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

    def list_runs(self, project_id: str) -> list[RunRecord]:
        self.get_project(project_id)
        runs: list[RunRecord] = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                run = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if run.project_id == project_id:
                runs.append(run)
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

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

    def search_evidence(
        self,
        project_id: str,
        *,
        query: str = "",
        kind: EvidenceKind | None = None,
        tag: str | None = None,
        status: EvidenceStatus | None = None,
        limit: int = 20,
    ) -> list[Evidence]:
        """Newest-first evidence search shared by the MCP tool and the HTTP API."""
        needle = query.strip().casefold()
        tag_needle = (tag or "").strip().casefold()
        matches: list[Evidence] = []
        for item in reversed(self.get_project(project_id).evidence):
            haystack = " ".join(
                [item.claim, item.source_title, item.excerpt or "", *item.tags]
            ).casefold()
            if needle and needle not in haystack:
                continue
            if kind and item.kind != kind:
                continue
            if tag_needle and tag_needle not in {value.casefold() for value in item.tags}:
                continue
            if status and item.status != status:
                continue
            matches.append(item)
            if len(matches) >= limit:
                break
        return matches

    def update_evidence_status(
        self,
        project_id: str,
        evidence_id: str,
        status: EvidenceStatus,
    ) -> tuple[Evidence, list[str]]:
        """FR-10 invalidation propagation.

        stale/removed: linked decisions record the evidence id in
        `invalidated_by_evidence_ids` and active decisions drop to needs_review.
        active: the id is cleared again; a decision returns to active only when no
        other invalidating evidence remains (a needs_review set by other causes is kept).
        """
        affected: list[str] = []
        updated: Evidence | None = None

        def mutate(project: OpportunityProject) -> None:
            nonlocal updated
            evidence = next(
                (item for item in project.evidence if item.id == evidence_id), None
            )
            if evidence is None:
                raise LedgerNotFoundError(evidence_id)
            evidence.status = status
            updated = evidence
            invalidating = status in {EvidenceStatus.STALE, EvidenceStatus.REMOVED}
            for decision in project.decisions:
                if evidence_id not in decision.evidence_ids:
                    continue
                if invalidating:
                    if evidence_id not in decision.invalidated_by_evidence_ids:
                        decision.invalidated_by_evidence_ids.append(evidence_id)
                    if decision.status == DecisionStatus.ACTIVE:
                        decision.status = DecisionStatus.NEEDS_REVIEW
                    affected.append(decision.id)
                elif evidence_id in decision.invalidated_by_evidence_ids:
                    decision.invalidated_by_evidence_ids.remove(evidence_id)
                    if (
                        not decision.invalidated_by_evidence_ids
                        and decision.status == DecisionStatus.NEEDS_REVIEW
                    ):
                        decision.status = DecisionStatus.ACTIVE
                    affected.append(decision.id)

        self.update_project(project_id, mutate)
        assert updated is not None
        return updated, affected

    def workspace_for_project(self, project_id: str) -> Path:
        self.get_project(project_id)
        workspace = self.projects_dir / self._safe_id(project_id) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename).name.strip().replace("\x00", "")
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
        if not name or name in {".", ".."}:
            raise ValueError("invalid artifact filename")
        return name[:240]

    def _artifact_path(self, project_id: str, relative_path: str) -> Path:
        workspace = self.workspace_for_project(project_id).resolve()
        candidate = (workspace / relative_path).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("artifact path escapes project workspace") from exc
        return candidate

    def _next_upload_path(self, project_id: str, filename: str) -> tuple[str, Path]:
        safe_name = self._safe_filename(filename)
        relative = Path("uploads") / safe_name
        path = self._artifact_path(project_id, relative.as_posix())
        path.parent.mkdir(parents=True, exist_ok=True)
        index = 2
        while path.exists():
            suffix = Path(safe_name).suffix
            stem = Path(safe_name).stem
            relative = Path("uploads") / f"{stem}.v{index}{suffix}"
            path = self._artifact_path(project_id, relative.as_posix())
            index += 1
        return relative.as_posix(), path

    def save_upload(
        self,
        project_id: str,
        filename: str,
        content: bytes,
        *,
        mime: str | None = None,
        run_id: str | None = None,
        source_ids: list[str] | None = None,
    ) -> Artifact:
        relative, path = self._next_upload_path(project_id, filename)
        path.write_bytes(content)
        return self.register_artifact(
            project_id,
            relative,
            run_id=run_id,
            source_ids=source_ids,
            mime=mime,
        )

    def register_artifact(
        self,
        project_id: str,
        relative_path: str,
        *,
        run_id: str | None = None,
        source_ids: list[str] | None = None,
        mime: str | None = None,
    ) -> Artifact:
        path = self._artifact_path(project_id, relative_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("artifact must be a regular file inside the project workspace")
        workspace = self.workspace_for_project(project_id).resolve()
        relative = path.relative_to(workspace).as_posix()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        project = self.get_project(project_id)
        duplicate = next(
            (
                item
                for item in project.artifacts
                if item.path == relative and item.checksum == checksum
            ),
            None,
        )
        if duplicate:
            return duplicate
        previous_versions = [item.version for item in project.artifacts if item.path == relative]
        artifact = Artifact(
            project_id=project_id,
            path=relative,
            name=path.name,
            mime=mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            version=max(previous_versions, default=0) + 1,
            run_id=run_id,
            source_ids=source_ids or [],
            checksum=checksum,
            size_bytes=path.stat().st_size,
            download_url="pending",
        )
        artifact.download_url = (
            f"/api/projects/{project_id}/artifacts/{artifact.id}/download"
        )
        self.update_project(project_id, lambda item: item.artifacts.append(artifact))
        return artifact

    def scan_workspace(self, project_id: str, *, run_id: str | None = None) -> list[Artifact]:
        workspace = self.workspace_for_project(project_id).resolve()
        registered: list[Artifact] = []
        for path in sorted(workspace.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(workspace).as_posix()
            except ValueError:
                continue
            registered.append(self.register_artifact(project_id, relative, run_id=run_id))
        return registered

    def get_artifact(self, project_id: str, artifact_id: str) -> tuple[Artifact, Path]:
        project = self.get_project(project_id)
        artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
        if artifact is None:
            raise LedgerNotFoundError(artifact_id)
        return artifact, self._artifact_path(project_id, artifact.path)

    def _agent_path(self, agent_id: str) -> Path:
        return self.agents_dir / self._safe_id(agent_id) / "agent.json"

    def create_agent(self, payload: AgentCreate) -> AgentProfile:
        agent = AgentProfile(**payload.model_dump())
        with self._lock_for(agent.id):
            self._atomic_write(self._agent_path(agent.id), agent)
        return agent

    def get_agent(self, agent_id: str) -> AgentProfile:
        path = self._agent_path(agent_id)
        if not path.exists():
            raise LedgerNotFoundError(agent_id)
        return AgentProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def list_agents(self) -> list[AgentProfile]:
        agents: list[AgentProfile] = []
        for path in self.agents_dir.glob("*/agent.json"):
            try:
                agents.append(AgentProfile.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(agents, key=lambda agent: agent.updated_at, reverse=True)

    def update_agent(self, agent_id: str, **changes: Any) -> AgentProfile:
        with self._lock_for(agent_id):
            agent = self.get_agent(agent_id)
            updated = agent.model_copy(update=changes)
            updated.revision = agent.revision + 1
            updated.updated_at = utc_now()
            self._atomic_write(self._agent_path(agent_id), updated)
            return updated

    def agent_core_dir(self, agent_id: str) -> Path:
        self.get_agent(agent_id)
        core_dir = self.agents_dir / self._safe_id(agent_id)
        core_dir.mkdir(parents=True, exist_ok=True)
        return core_dir

    def diary_dir(self, run: RunRecord) -> Path:
        """Agent runs journal into the agent core; legacy runs into the project."""
        if run.agent_id:
            base = self.agent_core_dir(run.agent_id)
        else:
            self.get_project(run.project_id)
            base = self.projects_dir / self._safe_id(run.project_id)
        diary = base / "diary"
        diary.mkdir(parents=True, exist_ok=True)
        return diary

    def _schedule_path(self, schedule_id: str) -> Path:
        return self.schedules_dir / self._safe_id(schedule_id) / "schedule.json"

    def create_schedule(self, schedule: Schedule) -> Schedule:
        self.get_project(schedule.project_id)
        with self._lock_for(schedule.id):
            self._atomic_write(self._schedule_path(schedule.id), schedule)
        return schedule

    def get_schedule(self, schedule_id: str) -> Schedule:
        path = self._schedule_path(schedule_id)
        if not path.exists():
            raise LedgerNotFoundError(schedule_id)
        return Schedule.model_validate_json(path.read_text(encoding="utf-8"))

    def list_schedules(self) -> list[Schedule]:
        schedules: list[Schedule] = []
        for path in self.schedules_dir.glob("*/schedule.json"):
            try:
                schedules.append(Schedule.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(schedules, key=lambda schedule: schedule.created_at, reverse=True)

    def update_schedule(self, schedule_id: str, **changes: Any) -> Schedule:
        with self._lock_for(schedule_id):
            schedule = self.get_schedule(schedule_id)
            updated = schedule.model_copy(update=changes)
            self._atomic_write(self._schedule_path(schedule_id), updated)
            return updated
