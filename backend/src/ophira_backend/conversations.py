"""Resolve a conversation from durable Run lineage, never from a client session ID."""

from collections.abc import Iterable

from .models import RunRecord
from .project_deletion_capture import ProjectDeletionConflict
from .storage import LedgerNotFoundError


def conversation_runs(
    project_id: str,
    run_id: str,
    all_runs: Iterable[RunRecord],
) -> list[RunRecord]:
    """Return the entire connected resume chain, including failed resume attempts.

    Retry-as-new runs have no resume link and remain independent. A changed SDK
    session ID does not split a chain whose durable source Run still links it.
    """
    records = list(all_runs)
    by_id = {run.id: run for run in records}
    if len(by_id) != len(records):
        raise ProjectDeletionConflict("Conversation Run identities are ambiguous")
    source = by_id.get(run_id)
    if source is None or source.project_id != project_id:
        raise LedgerNotFoundError(run_id)
    selected = {run_id}
    sessions = set()
    changed = True
    while changed:
        changed = False
        for run in records:
            keys = {
                (run.provider_profile_id or "environment", sid)
                for sid in (run.sdk_session_id, run.resume_session_id)
                if sid
            }
            linked = (
                run.id in selected or run.resume_source_run_id in selected or bool(keys & sessions)
            )
            if not linked:
                continue
            if run.project_id != project_id:
                raise ProjectDeletionConflict("Conversation lineage crosses project boundaries")
            if run.resume_source_run_id:
                parent = by_id.get(run.resume_source_run_id)
                if parent is None or parent.project_id != project_id:
                    raise ProjectDeletionConflict("Conversation source Run cannot be verified")
                if parent.id not in selected:
                    selected.add(parent.id)
                    changed = True
            if run.id not in selected or not keys <= sessions:
                selected.add(run.id)
                sessions.update(keys)
                changed = True
    return sorted(
        (by_id[identifier] for identifier in selected), key=lambda run: (run.created_at, run.id)
    )
