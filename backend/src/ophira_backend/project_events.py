"""Recoverable observations of committed, Run-bound project records.

Project records remain authoritative. This projector never repeats a business
write and never reconstructs a record from model output or an event payload.
"""

from __future__ import annotations

import logging

from .constants import TERMINAL_RUN_STATUSES, EventType, MemoryScope, MemorySource, RunStatus
from .events import EventPublisher
from .memory import MemoryProvider
from .models import Artifact, Decision, EventActor, Evidence, MemoryItem, RunRecord
from .scoped_events import ScopedEventStore
from .storage import FileLedger, LedgerNotFoundError

logger = logging.getLogger(__name__)


class ProjectEventProjector:
    def __init__(
        self, ledger: FileLedger, events: EventPublisher, memory: MemoryProvider | None = None,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.memory = memory
        self.scoped = ScopedEventStore(ledger)

    def reconcile_project(self, project_id: str) -> None:
        """Invalidate views from committed revisions, including manual writes.

        Downtime may coalesce several revisions into one observation. The current
        ledger is authoritative; this is not an invented edit-by-edit history.
        """
        # Serialize snapshot reading and observation across workers, so an old
        # reader cannot append a lower revision after a newer one.
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            project = self.ledger.get_project(project_id)
            stream = self.scoped.project_stream(project_id)
            observed = self.scoped.observed_keys(stream)
            revision_key = f"revision-{project.revision}"
            if (EventType.PROJECT_CHANGED, revision_key) not in observed:
                self.scoped.append(
                    stream, EventType.PROJECT_CHANGED, revision_key,
                    {"schema_version": 1, "revision": project.revision,
                     "recorded_at": project.updated_at.isoformat()},
                    actor=EventActor(type="system", name="project-ledger"),
                )
        for run in self.ledger.list_runs(project_id, sort_by_created_at=False):
            self.observe_schedule_run(run, observed=observed)

    def observe_schedule_run(
        self, run: RunRecord, *, observed: set[tuple[str, str]] | None = None,
    ) -> None:
        if not run.schedule_id:
            return
        stream = self.scoped.project_stream(run.project_id)
        data = {"schema_version": 1, "schedule_id": run.schedule_id,
                "run_id": run.id, "automation_claim_id": run.automation_claim_id}
        actor = EventActor(type="system", name="scheduler")
        if observed is None or (EventType.SCHEDULE_FIRED, run.id) not in observed:
            self.scoped.append(
                stream, EventType.SCHEDULE_FIRED, run.id,
                {**data, "status": "started", "recorded_at": run.created_at.isoformat()},
                run_id=run.id, actor=actor,
            )
        if run.status in TERMINAL_RUN_STATUSES:
            event_type = {
                RunStatus.COMPLETED: EventType.SCHEDULE_COMPLETED,
                RunStatus.FAILED: EventType.SCHEDULE_FAILED,
                RunStatus.CANCELLED: EventType.SCHEDULE_CANCELLED,
            }[run.status]
            if observed is None or (event_type, run.id) not in observed:
                self.scoped.append(
                    stream, event_type, run.id, {**data, "status": run.status.value},
                    run_id=run.id, actor=actor,
                )

    async def artifact_saved(self, project_id: str, artifact_id: str) -> bool:
        try:
            project = self.ledger.get_project(project_id)
            record = next((item for item in project.artifacts if item.id == artifact_id), None)
            return True if record is None else await self._publish_record(project_id, record)
        except Exception as exc:
            self._log_failure(exc)
            return False

    async def memory_proposed(self, run_id: str, memory_id: str) -> bool:
        try:
            run = self.ledger.get_run(run_id)
            record = next((item for item in self._run_memories(run) if item.id == memory_id), None)
            return True if record is None else await self._publish_memory(run, record)
        except Exception as exc:
            self._log_failure(exc)
            return False

    def _run_memories(self, run: RunRecord) -> list[MemoryItem]:
        if self.memory is None:
            return []
        pairs = [(MemoryScope.PROJECT, run.project_id)]
        if run.agent_id:
            pairs.append((MemoryScope.AGENT, run.agent_id))
        return [
            item for scope, scope_id in pairs
            for item in self.memory.list(scope=scope, scope_id=scope_id)
            if item.scope == scope and item.scope_id == scope_id and item.source_run_id == run.id
        ]

    async def _publish_memory(self, run: RunRecord, item: MemoryItem) -> bool:
        # A project-scoped event may be read by other project members. Carry no
        # memory prose, scope, identity or approval state, including on replay.
        if item.source not in {MemorySource.AGENT_PROPOSAL, MemorySource.DISTILLATION}:
            return True
        if any(getattr(item, key) != getattr(run, key) for key in (
            "identity_binding", "tenant_id", "principal_id", "principal_role",
            "principal_session_id",
        )):
            return True
        try:
            await self.events.publish_once(
                run.id, EventType.MEMORY_PROPOSED,
                {
                    "schema_version": 1, "resource_id": item.id, "memory_id": item.id,
                    "recorded_at": item.created_at.isoformat(),
                },
                resource_id=item.id, actor=EventActor(type="system", name="memory-ledger"),
            )
        except Exception as exc:
            self._log_failure(exc)
            return False
        return True

    async def evidence_added(self, project_id: str, evidence_id: str) -> bool:
        """Observe an existing record, including one returned by a duplicate call."""
        return await self._observe_record(project_id, evidence_id, evidence=True)

    async def decision_recorded(self, project_id: str, decision_id: str) -> bool:
        return await self._observe_record(project_id, decision_id, evidence=False)

    async def _observe_record(
        self, project_id: str, resource_id: str, *, evidence: bool,
    ) -> bool:
        try:
            project = self.ledger.get_project(project_id)
            records = project.evidence if evidence else project.decisions
            record = next((item for item in records if item.id == resource_id), None)
            # An ID alone is not proof that a business write succeeded.
            if record is None:
                return True
            return await self._publish_record(project_id, record)
        except Exception as exc:
            self._log_failure(exc)
            return False

    async def reconcile_run(self, run_id: str) -> bool:
        """Confirm all attributable records have a durable observation.

        True includes notices already present and legacy records without usable
        provenance, which are deliberately skipped. False means at least one
        projection failed; callers must not claim the stream is fully reconciled.
        Event sequence is observation order, never a fabricated business timeline.
        """
        try:
            run = self.ledger.get_run(run_id)
            project = self.ledger.get_project(run.project_id)
        except Exception as exc:
            self._log_failure(exc)
            return False
        complete = True
        try:
            self.observe_schedule_run(run)
        except Exception as exc:
            self._log_failure(exc)
            complete = False
        for record in [*project.evidence, *project.decisions, *project.artifacts]:
            source_id = record.run_id if isinstance(record, Artifact) else record.source_run_id
            if source_id == run.id:
                # Do not short-circuit: one failed notification must not suppress
                # observations of other records already committed to the ledger.
                confirmed = await self._publish_record(project.id, record)
                complete = confirmed and complete
        try:
            for item in self._run_memories(run):
                confirmed = await self._publish_memory(run, item)
                complete = confirmed and complete
        except Exception as exc:
            self._log_failure(exc)
            complete = False
        return complete

    async def _publish_record(
        self, project_id: str, record: Evidence | Decision | Artifact,
    ) -> bool:
        source_id = record.run_id if isinstance(record, Artifact) else record.source_run_id
        if not source_id:
            return True
        try:
            source_run = self.ledger.get_run(source_id)
        except LedgerNotFoundError:
            # Historical/imported provenance cannot justify inventing a Run.
            return True
        except Exception as exc:
            self._log_failure(exc)
            return False
        if source_run.project_id != project_id:
            return True
        extra = {}
        if isinstance(record, Artifact):
            if record.project_id != project_id:
                return True
            event_type = (EventType.ARTIFACT_UPDATED if record.version > 1
                          else EventType.ARTIFACT_REGISTERED)
            resource_field = "artifact_id"
            recorded_at = record.created_at
            extra = {"version": record.version}
        elif isinstance(record, Evidence):
            event_type = EventType.EVIDENCE_ADDED
            resource_field = "evidence_id"
            recorded_at = record.recorded_at
        else:
            event_type = EventType.DECISION_RECORDED
            resource_field = "decision_id"
            recorded_at = record.created_at
        try:
            await self.events.publish_once(
                source_run.id,
                event_type,
                {
                    "schema_version": 1,
                    "resource_id": record.id,
                    resource_field: record.id,
                    "recorded_at": recorded_at.isoformat(),
                    **extra,
                },
                resource_id=record.id,
                actor=EventActor(type="system", name="project-ledger"),
            )
        except Exception as exc:
            self._log_failure(exc)
            return False
        return True

    @staticmethod
    def _log_failure(exc: Exception) -> None:
        # Exception text, resource prose and caller-supplied identifiers can carry
        # secrets or instructions. Keep diagnostics useful without copying them.
        logger.warning("Project event projection failed (%s)", type(exc).__name__)
