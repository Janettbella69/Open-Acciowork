"""Recoverable observations of committed, Run-bound project records.

Project records remain authoritative. This projector never repeats a business
write and never reconstructs a record from model output or an event payload.
"""

from __future__ import annotations

import logging

from .constants import EventType
from .events import EventPublisher
from .models import Decision, EventActor, Evidence
from .storage import FileLedger, LedgerNotFoundError

logger = logging.getLogger(__name__)


class ProjectEventProjector:
    def __init__(self, ledger: FileLedger, events: EventPublisher) -> None:
        self.ledger = ledger
        self.events = events

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
        for record in [*project.evidence, *project.decisions]:
            if record.source_run_id == run.id:
                # Do not short-circuit: one failed notification must not suppress
                # observations of other records already committed to the ledger.
                confirmed = await self._publish_record(project.id, record)
                complete = confirmed and complete
        return complete

    async def _publish_record(self, project_id: str, record: Evidence | Decision) -> bool:
        if not record.source_run_id:
            return True
        try:
            source_run = self.ledger.get_run(record.source_run_id)
        except LedgerNotFoundError:
            # Historical/imported provenance cannot justify inventing a Run.
            return True
        except Exception as exc:
            self._log_failure(exc)
            return False
        if source_run.project_id != project_id:
            return True
        if isinstance(record, Evidence):
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
