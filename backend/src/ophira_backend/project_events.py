"""Recoverable observations of committed, Run-bound project records.

Project records remain authoritative. This projector never repeats a business
write and never reconstructs a record from model output or an event payload.
"""

from __future__ import annotations

import logging

from .constants import EventType, MemoryScope
from .events import EventPublisher
from .memory import MemoryNotFoundError, MemoryProvider, memory_visible_to
from .models import Artifact, Decision, EventActor, Evidence, MemoryItem
from .storage import FileLedger, LedgerNotFoundError

logger = logging.getLogger(__name__)


class ProjectEventProjector:
    def __init__(
        self, ledger: FileLedger, events: EventPublisher, *, memory: MemoryProvider | None = None,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.memory = memory

    async def evidence_added(self, project_id: str, evidence_id: str) -> bool:
        """Observe an existing record, including one returned by a duplicate call."""
        return await self._observe_record(project_id, evidence_id, collection="evidence")

    async def decision_recorded(self, project_id: str, decision_id: str) -> bool:
        return await self._observe_record(project_id, decision_id, collection="decisions")

    async def artifact_registered(self, project_id: str, artifact_id: str) -> bool:
        return await self._observe_record(project_id, artifact_id, collection="artifacts")

    async def memory_proposed(self, project_id: str, memory_id: str) -> bool:
        if self.memory is None:
            return True
        try:
            record = self.memory.get(memory_id)
            return await self._publish_record(project_id, record)
        except MemoryNotFoundError:
            # Deletion wins over a delayed observation. Do not recover deleted prose.
            return True
        except Exception as exc:
            self._log_failure(exc)
            return False

    async def _observe_record(
        self, project_id: str, resource_id: str, *, collection: str,
    ) -> bool:
        try:
            project = self.ledger.get_project(project_id)
            records = getattr(project, collection)
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
        for record in [*project.evidence, *project.decisions, *project.artifacts]:
            source_run_id = record.run_id if isinstance(record, Artifact) else record.source_run_id
            if source_run_id == run.id:
                # Do not short-circuit: one failed notification must not suppress
                # observations of other records already committed to the ledger.
                confirmed = await self._publish_record(project.id, record)
                complete = confirmed and complete
        if self.memory is not None:
            pairs = [(MemoryScope.PROJECT, project.id)]
            if run.agent_id:
                pairs.append((MemoryScope.AGENT, run.agent_id))
            for scope, scope_id in pairs:
                try:
                    for item in self.memory.list(scope=scope, scope_id=scope_id):
                        if item.source_run_id == run.id:
                            # Re-read so a deletion after enumeration is respected.
                            confirmed = await self.memory_proposed(project.id, item.id)
                            complete = confirmed and complete
                except Exception as exc:
                    self._log_failure(exc)
                    complete = False
        return complete

    async def _publish_record(
        self, project_id: str, record: Evidence | Decision | Artifact | MemoryItem,
    ) -> bool:
        source_run_id = record.run_id if isinstance(record, Artifact) else record.source_run_id
        if not source_run_id:
            return True
        try:
            source_run = self.ledger.get_run(source_run_id)
        except LedgerNotFoundError:
            # Historical/imported provenance cannot justify inventing a Run.
            return True
        except Exception as exc:
            self._log_failure(exc)
            return False
        if source_run.project_id != project_id:
            return True
        if isinstance(record, Artifact) and record.project_id != project_id:
            return True
        if isinstance(record, MemoryItem):
            expected_scope = (
                record.scope == MemoryScope.PROJECT and record.scope_id == project_id
            ) or (
                record.scope == MemoryScope.AGENT and record.scope_id == source_run.agent_id
            )
            if (not expected_scope
                    or not memory_visible_to(record, source_run)
                    or record.principal_id != source_run.principal_id):
                return True
        if isinstance(record, Evidence):
            event_type = EventType.EVIDENCE_ADDED
            resource_field = "evidence_id"
            recorded_at = record.recorded_at
        elif isinstance(record, Decision):
            event_type = EventType.DECISION_RECORDED
            resource_field = "decision_id"
            recorded_at = record.created_at
        elif isinstance(record, Artifact):
            event_type = EventType.ARTIFACT_REGISTERED
            resource_field = "artifact_id"
            recorded_at = record.created_at
        else:
            event_type = EventType.MEMORY_PROPOSED
            resource_field = "memory_id"
            recorded_at = record.created_at
        payload = {
            "schema_version": 1,
            "resource_id": record.id,
            resource_field: record.id,
            "recorded_at": recorded_at.isoformat(),
        }
        if isinstance(record, Artifact):
            # Preserve the existing registered payload contract. New update and
            # memory notices contain identifiers only, never file or memory prose.
            payload = {**record.model_dump(mode="json"), **payload}
        try:
            await self.events.publish_once(
                source_run.id,
                event_type,
                payload,
                resource_id=record.id,
                actor=EventActor(type="system", name="project-ledger"),
            )
            if isinstance(record, Artifact) and record.version > 1:
                await self.events.publish_once(
                    source_run.id, EventType.ARTIFACT_UPDATED,
                    {
                        "schema_version": 1, "resource_id": record.id,
                        "artifact_id": record.id, "version": record.version,
                        "recorded_at": recorded_at.isoformat(),
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
