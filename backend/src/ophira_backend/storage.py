from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .constants import (
    DecisionStatus,
    EvidenceKind,
    EvidenceSourceType,
    EvidenceStatus,
    RunMode,
)
from .ingest import ingest_to_markdown, needs_markdown_companion
from .models import (
    AgentCreate,
    AgentProfile,
    Artifact,
    EventEnvelope,
    Evidence,
    OpportunityProject,
    PrincipalBinding,
    PrincipalContext,
    ProjectCreate,
    RunRecord,
    RunSummary,
    Schedule,
    ToolReceipt,
    ToolReceiptReplay,
    new_id,
    utc_now,
)


class LedgerNotFoundError(KeyError):
    pass


class LedgerIdentityScopeError(RuntimeError):
    """A durable object was accessed or created outside its principal scope."""


class SourceReceiptError(RuntimeError):
    """Base error for server-issued source receipt validation."""


class SourceReceiptNotFoundError(SourceReceiptError):
    pass


class SourceReceiptScopeError(SourceReceiptError):
    pass


class SourceReceiptExpiredError(SourceReceiptError):
    pass


class SourceReceiptIntegrityError(SourceReceiptError):
    pass


class FileLedger:
    """Durable JSON/JSONL ledger with atomic snapshots and append-only detail logs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.projects_dir = self.root / "projects"
        self.runs_dir = self.root / "runs"
        self.agents_dir = self.root / "agents"
        self.schedules_dir = self.root / "schedules"
        self.receipts_dir = self.root / "receipts"
        self.manifest_path = self.root / "_manifest.jsonl"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
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

    def _receipt_directory(self, project_id: str, receipt_id: str) -> Path:
        return (
            self.receipts_dir
            / self._safe_id(project_id)
            / self._safe_id(receipt_id)
        )

    @staticmethod
    def _canonical_source(value: str) -> str:
        """Normalize a host-observed source without accepting ambiguous credentials."""

        source = value.strip()
        if not source or len(source) > 2_000 or any(ord(char) < 32 for char in source):
            raise SourceReceiptIntegrityError("canonical source is invalid")
        try:
            parts = urlsplit(source)
        except ValueError as exc:
            raise SourceReceiptIntegrityError("canonical source is invalid") from exc
        scheme = parts.scheme.casefold()
        if scheme in {"http", "https"}:
            if not parts.hostname or parts.username is not None or parts.password is not None:
                raise SourceReceiptIntegrityError("canonical HTTP source is invalid")
            try:
                hostname = parts.hostname.rstrip(".").casefold()
                if ":" in hostname:
                    hostname = f"[{hostname}]"
                else:
                    hostname = hostname.encode("idna").decode("ascii")
                port = parts.port
            except (UnicodeError, ValueError) as exc:
                raise SourceReceiptIntegrityError("canonical HTTP source is invalid") from exc
            default_port = 443 if scheme == "https" else 80
            netloc = hostname + (f":{port}" if port is not None and port != default_port else "")
            return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        if scheme == "ophira-search" and parts.netloc and not parts.fragment:
            return urlunsplit((scheme, parts.netloc.casefold(), parts.path, parts.query, ""))
        raise SourceReceiptIntegrityError("canonical source scheme is not supported")

    @staticmethod
    def _canonical_payload_bytes(payload: Any) -> bytes:
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SourceReceiptIntegrityError(
                "source receipt payload must be finite JSON"
            ) from exc

    def issue_tool_receipt(
        self,
        project_id: str,
        run_id: str,
        *,
        tool: str,
        source_type: EvidenceSourceType,
        canonical_source: str,
        payload: Any,
        object_type: str,
        object_key: str | None = None,
        object_version: str | None = None,
        object_metadata: dict[str, Any] | None = None,
        ttl: timedelta = timedelta(hours=24),
        tenant_id: str | None = None,
        principal_id: str | None = None,
        now: datetime | None = None,
    ) -> ToolReceipt:
        """Issue a receipt from trusted host/adapter code, never from an MCP input.

        The initialized SDK session is read from the durable run.  Callers cannot
        nominate another session and thereby broaden the writable provenance set.
        """

        try:
            project = self.get_project(project_id)
            run = self.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise SourceReceiptScopeError("source receipt run scope is not valid") from exc
        if run.project_id != project_id or not run.sdk_session_id:
            raise SourceReceiptScopeError(
                "source receipt requires this project's initialized SDK session"
            )
        if project.identity_binding != run.identity_binding:
            raise SourceReceiptScopeError("source receipt project/run identity does not match")
        if run.identity_binding == "authenticated_principal":
            if (
                project.tenant_id != run.tenant_id
                or tenant_id not in {None, run.tenant_id}
                or principal_id not in {None, run.principal_id}
            ):
                raise SourceReceiptScopeError(
                    "source receipt principal scope is not valid for this run"
                )
            tenant_id = run.tenant_id
            principal_id = run.principal_id
        elif tenant_id is not None or principal_id is not None:
            raise SourceReceiptScopeError(
                "local-only runs cannot mint authenticated source receipts"
            )
        if ttl <= timedelta(0) or ttl > timedelta(days=30):
            raise SourceReceiptIntegrityError("source receipt TTL is invalid")
        canonical = self._canonical_source(canonical_source)
        encoded = self._canonical_payload_bytes(payload)
        content_hash = hashlib.sha256(encoded).hexdigest()
        metadata = object_metadata or {}
        if len(self._canonical_payload_bytes(metadata)) > 16 * 1024:
            raise SourceReceiptIntegrityError("source receipt metadata exceeds 16 KB")
        issued_at = now or utc_now()
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            raise SourceReceiptIntegrityError("source receipt clock must be timezone-aware")
        identity_binding = (
            "authenticated_principal"
            if tenant_id is not None and principal_id is not None
            else "project_run_session_only"
        )

        with self._lock_for(f"receipt:{project_id}"):
            receipt_id = new_id("receipt")
            directory = self._receipt_directory(project_id, receipt_id)
            while directory.exists():  # pragma: no cover - UUID collision defense
                receipt_id = new_id("receipt")
                directory = self._receipt_directory(project_id, receipt_id)
            relative_payload = (
                Path("receipts") / project_id / receipt_id / "payload.json"
            ).as_posix()
            receipt = ToolReceipt(
                id=receipt_id,
                project_id=project_id,
                run_id=run.id,
                session_id=run.sdk_session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                principal_role=run.principal_role,
                principal_session_id=run.principal_session_id,
                identity_binding=identity_binding,
                tool=tool,
                source_type=EvidenceSourceType(source_type),
                canonical_source=canonical,
                retrieved_at=issued_at,
                content_hash=content_hash,
                object_type=object_type,
                object_key=object_key or canonical,
                object_version=object_version or f"sha256:{content_hash}",
                object_metadata=metadata,
                sanitized_payload_ref=relative_payload,
                expires_at=issued_at + ttl,
                created_at=issued_at,
            )
            self._atomic_write(directory / "payload.json", payload)
            # Publish metadata only after the payload exists.  A crash before
            # this write leaves an unreachable payload, never a valid receipt.
            self._atomic_write(directory / "receipt.json", receipt)
        return receipt

    def _read_tool_receipt(self, project_id: str, receipt_id: str) -> ToolReceipt:
        try:
            directory = self._receipt_directory(project_id, receipt_id)
        except ValueError as exc:
            raise SourceReceiptNotFoundError("source receipt not found") from exc
        if directory.is_symlink():
            raise SourceReceiptIntegrityError("source receipt directory is invalid")
        try:
            directory.resolve().relative_to(self.receipts_dir.resolve())
        except (OSError, ValueError) as exc:
            raise SourceReceiptIntegrityError(
                "source receipt directory escapes the receipt store"
            ) from exc
        path = directory / "receipt.json"
        try:
            if path.is_symlink() or not path.is_file():
                raise SourceReceiptNotFoundError("source receipt not found")
            receipt = ToolReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        except SourceReceiptNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceReceiptIntegrityError(
                "stored source receipt failed integrity validation"
            ) from exc
        expected_ref = (
            Path("receipts") / project_id / receipt_id / "payload.json"
        ).as_posix()
        if (
            receipt.id != receipt_id
            or receipt.project_id != project_id
            or receipt.sanitized_payload_ref != expected_ref
        ):
            raise SourceReceiptIntegrityError(
                "stored source receipt does not match its ledger scope"
            )
        return receipt

    def replay_tool_receipt(
        self,
        project_id: str,
        receipt_id: str,
        *,
        now: datetime | None = None,
    ) -> ToolReceiptReplay:
        """Replay an immutable sanitized payload and verify its content hash."""

        receipt = self._read_tool_receipt(project_id, receipt_id)
        payload_path = self._receipt_directory(project_id, receipt_id) / "payload.json"
        try:
            if payload_path.is_symlink() or not payload_path.is_file():
                raise SourceReceiptIntegrityError("source receipt payload is unavailable")
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except SourceReceiptIntegrityError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceReceiptIntegrityError(
                "source receipt payload failed integrity validation"
            ) from exc
        actual_hash = hashlib.sha256(self._canonical_payload_bytes(payload)).hexdigest()
        if actual_hash != receipt.content_hash:
            raise SourceReceiptIntegrityError("source receipt payload hash does not match")
        if (
            receipt.object_version.startswith("sha256:")
            and receipt.object_version != f"sha256:{actual_hash}"
        ):
            raise SourceReceiptIntegrityError("source receipt object version does not match")
        checked_at = now or utc_now()
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise SourceReceiptIntegrityError(
                "source receipt verification clock must be timezone-aware"
            )
        return ToolReceiptReplay(
            receipt=receipt,
            payload=payload,
            expired=checked_at >= receipt.expires_at,
        )

    def verify_tool_receipt(
        self,
        project_id: str,
        receipt_id: str,
        *,
        run_id: str,
        session_id: str,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        now: datetime | None = None,
    ) -> ToolReceiptReplay:
        """Verify integrity, freshness, and the complete current write scope."""

        replay = self.replay_tool_receipt(project_id, receipt_id, now=now)
        receipt = replay.receipt
        try:
            run = self.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise SourceReceiptScopeError(
                "source receipt run scope is not valid"
            ) from exc
        if run.project_id != project_id or run.sdk_session_id != session_id:
            raise SourceReceiptScopeError(
                "source receipt is not valid for this project/run/session"
            )
        if tenant_id not in {None, run.tenant_id} or principal_id not in {
            None,
            run.principal_id,
        }:
            raise SourceReceiptScopeError(
                "source receipt is not valid for this principal"
            )
        if (
            receipt.run_id != run_id
            or receipt.session_id != session_id
            or receipt.tenant_id != run.tenant_id
            or receipt.principal_id != run.principal_id
            or receipt.principal_role != run.principal_role
            or receipt.principal_session_id != run.principal_session_id
        ):
            raise SourceReceiptScopeError(
                "source receipt is not valid for this project/run/session"
            )
        if replay.expired:
            raise SourceReceiptExpiredError("source receipt has expired")
        return replay

    def list_tool_receipts(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
    ) -> list[ToolReceipt]:
        self.get_project(project_id)
        project_dir = self.receipts_dir / self._safe_id(project_id)
        if not project_dir.exists():
            return []
        if project_dir.is_symlink() or not project_dir.is_dir():
            raise SourceReceiptIntegrityError("source receipt directory is invalid")
        receipts: list[ToolReceipt] = []
        for path in project_dir.glob("receipt_*/receipt.json"):
            replay = self.replay_tool_receipt(project_id, path.parent.name)
            if run_id is None or replay.receipt.run_id == run_id:
                receipts.append(replay.receipt)
        return sorted(receipts, key=lambda item: item.retrieved_at, reverse=True)

    def create_project(
        self,
        payload: ProjectCreate,
        *,
        principal: PrincipalContext | PrincipalBinding | None = None,
        project_id: str | None = None,
    ) -> OpportunityProject:
        identity: dict[str, Any] = {}
        if principal is not None:
            if principal.role.value != "owner":
                raise LedgerIdentityScopeError(
                    "a new authenticated project requires an owner principal"
                )
            identity = {
                "tenant_id": principal.tenant_id,
                "owner_principal_id": principal.principal_id,
                "owner_role": principal.role,
                "owner_session_id": principal.session_id,
                "identity_binding": "authenticated_principal",
            }
        project = OpportunityProject(
            **payload.model_dump(),
            **identity,
            **({"id": self._safe_id(project_id)} if project_id is not None else {}),
        )
        with self._lock_for(project.id):
            self._atomic_write(self._project_path(project.id), project)
        return project

    def bind_project_identity(
        self,
        project_id: str,
        principal: PrincipalContext,
    ) -> OpportunityProject:
        """Explicitly migrate one local project; never infer ownership from a caller."""

        if principal.role.value != "owner":
            raise LedgerIdentityScopeError(
                "project migration requires an authenticated owner principal"
            )

        def bind(project: OpportunityProject) -> None:
            if project.identity_binding == "authenticated_principal":
                if (
                    project.tenant_id == principal.tenant_id
                    and project.owner_principal_id == principal.principal_id
                ):
                    return
                raise LedgerIdentityScopeError("project is already bound to another identity")
            project.tenant_id = principal.tenant_id
            project.owner_principal_id = principal.principal_id
            project.owner_role = principal.role
            project.owner_session_id = principal.session_id
            project.identity_binding = "authenticated_principal"

        return self.update_project(project_id, bind)

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
        standing_intent_id: str | None = None,
        automation_claim_id: str | None = None,
        resume_session_id: str | None = None,
        resume_source_run_id: str | None = None,
        session_compaction_epoch: int = 0,
        mode: RunMode = RunMode.STANDARD,
        provider_profile_id: str | None = None,
        provider_snapshot: dict[str, Any] | None = None,
        skill_snapshot: list[dict[str, Any]] | None = None,
        max_budget_usd: float | None = None,
        principal: PrincipalContext | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        project = self.get_project(project_id)
        identity: dict[str, Any] = {}
        if project.identity_binding == "authenticated_principal":
            if principal is None:
                raise LedgerIdentityScopeError(
                    "authenticated projects require a trusted principal for every run"
                )
            if project.tenant_id != principal.tenant_id:
                raise LedgerIdentityScopeError("run principal is outside the project tenant")
            identity = {
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "principal_role": principal.role,
                "principal_session_id": principal.session_id,
                "identity_binding": "authenticated_principal",
            }
        elif principal is not None:
            raise LedgerIdentityScopeError(
                "bind the local project before creating an authenticated run"
            )
        run = RunRecord(
            project_id=project_id,
            prompt=prompt,
            **identity,
            **({"id": self._safe_id(run_id)} if run_id is not None else {}),
            agent_id=agent_id,
            schedule_id=schedule_id,
            standing_intent_id=standing_intent_id,
            automation_claim_id=automation_claim_id,
            resume_session_id=resume_session_id,
            resume_source_run_id=resume_source_run_id,
            session_compaction_epoch=session_compaction_epoch,
            mode=mode,
            provider_profile_id=provider_profile_id,
            provider_snapshot=provider_snapshot,
            skill_snapshot=skill_snapshot or [],
            max_budget_usd=max_budget_usd,
        )
        summary = RunSummary(
            run_id=run.id,
            project_id=project_id,
            trace_id=run.trace_id,
            status=run.status,
        )
        with self._lock_for(run.id):
            if self._run_path(run.id).exists():
                raise ValueError("run already exists")
            self._atomic_write(self._run_path(run.id), run)
            self._atomic_write(self._summary_path(run.id), summary)
            self._append_manifest(run, 0)
        return run

    def get_run(self, run_id: str) -> RunRecord:
        path = self._run_path(run_id)
        if not path.exists():
            raise LedgerNotFoundError(run_id)
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, project_id: str | None = None) -> list[RunRecord]:
        if project_id is not None:
            self.get_project(project_id)
        runs = [
            run for run in self._iter_runs()
            if project_id is None or run.project_id == project_id
        ]
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

    def _iter_runs(self) -> list[RunRecord]:
        runs: list[RunRecord] = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                run = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            runs.append(run)
        return runs

    def runs_for_sdk_session(self, session_id: str) -> list[RunRecord]:
        """Return durable runs bound to an SDK session, newest first."""

        if not session_id or len(session_id) > 500:
            raise ValueError("invalid SDK session identifier")
        matches = [run for run in self._iter_runs() if run.sdk_session_id == session_id]
        return sorted(matches, key=lambda run: run.created_at, reverse=True)

    def runs_claiming_sdk_session(self, session_id: str) -> list[RunRecord]:
        """Include queued resume attempts so one transcript cannot be resumed concurrently."""

        if not session_id or len(session_id) > 500:
            raise ValueError("invalid SDK session identifier")
        matches = [
            run
            for run in self._iter_runs()
            if run.sdk_session_id == session_id or run.resume_session_id == session_id
        ]
        return sorted(matches, key=lambda run: run.created_at, reverse=True)

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
        artifact = self.register_artifact(
            project_id,
            relative,
            run_id=run_id,
            source_ids=source_ids,
            mime=mime,
        )
        if needs_markdown_companion(filename):
            markdown, _backend = ingest_to_markdown(path, original_name=filename)
            companion = f"{relative}.md"
            companion_path = self._artifact_path(project_id, companion)
            companion_path.parent.mkdir(parents=True, exist_ok=True)
            companion_path.write_text(markdown, encoding="utf-8")
            self.register_artifact(
                project_id,
                companion,
                run_id=run_id,
                source_ids=[artifact.id],
                mime="text/markdown",
            )
        return artifact

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
