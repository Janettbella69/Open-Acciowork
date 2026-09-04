"""Memory distillation: diary + project truth → MemoryProposal only (FR-14).

Untrusted origins (web files, community signals) are structurally excluded
before a candidate is created. No second model call — extraction is
deterministic from ledger fields the main Agent already wrote.
"""

from __future__ import annotations

from .constants import EvidenceKind, MemoryKind, MemoryOrigin, MemoryScope, MemorySource
from .memory import FileMemoryProvider
from .models import MemoryItem, OpportunityProject, RunRecord
from .storage import FileLedger


def distill_run(
    ledger: FileLedger,
    memory: FileMemoryProvider,
    run: RunRecord,
) -> list[MemoryItem]:
    """Propose memories from user-owned project truth recorded around this run."""
    existing = {
        item.text
        for item in memory.list(scope=MemoryScope.PROJECT, scope_id=run.project_id)
    }
    project = ledger.get_project(run.project_id)
    proposed: list[MemoryItem] = []
    for text, kind, origin, anchor in _candidates(project, run):
        if origin == MemoryOrigin.UNTRUSTED or text in existing:
            continue
        item = memory.propose(
            scope=MemoryScope.PROJECT,
            scope_id=run.project_id,
            text=text[:4000],
            kind=kind,
            source=MemorySource.DISTILLATION,
            created_by="distill",
            origin=origin,
            source_run_id=run.id,
            source_anchor=anchor,
            principal=run,
        )
        proposed.append(item)
    return proposed


def _candidates(
    project: OpportunityProject, run: RunRecord
) -> list[tuple[str, MemoryKind, MemoryOrigin, str]]:
    rows: list[tuple[str, MemoryKind, MemoryOrigin, str]] = []
    for decision in project.decisions:
        if decision.source_run_id != run.id:
            continue
        if decision.made_by in {"test", "system"}:
            continue
        text = f"结论：{decision.question} → {decision.outcome}"
        if decision.rationale:
            text += f"。理由：{decision.rationale[:400]}"
        rows.append((text, MemoryKind.DECISION, MemoryOrigin.AGENT, decision.id))
    for evidence in project.evidence:
        if evidence.source_run_id != run.id:
            continue
        if evidence.kind == EvidenceKind.USER_STATEMENT:
            rows.append(
                (
                    f"用户陈述：{evidence.claim}",
                    MemoryKind.FACT,
                    MemoryOrigin.OWNER,
                    evidence.id,
                )
            )
    # Deduplicate against existing texts.
    seen: set[str] = set()
    unique: list[tuple[str, MemoryKind, MemoryOrigin, str]] = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        unique.append(row)
    return unique[:8]
