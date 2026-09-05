"""FR-10: marking evidence stale/removed must flag decisions that relied on it,
and restoring the evidence must un-flag them (only when nothing else invalidates)."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import (
    DecisionStatus,
    EvidenceKind,
    EvidenceSourceType,
    EvidenceStatus,
)
from ophira_backend.main import create_app
from ophira_backend.models import Decision, Evidence, ProjectCreate
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def seed_project(ledger: FileLedger):
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    evidence_a = Evidence(
        kind=EvidenceKind.OBSERVED_FACT,
        claim="报价单显示单价 6.8 USD",
        source_title="quotes.csv",
        source_type=EvidenceSourceType.FILE,
        source_path="uploads/quotes.csv",
        confidence=0.9,
        recorded_by="test",
    )
    evidence_b = Evidence(
        kind=EvidenceKind.COMMUNITY_SIGNAL,
        claim="社媒热度上升",
        source_title="signal",
        source_url="https://example.org/signal",
        source_type=EvidenceSourceType.WEB,
        confidence=0.4,
        recorded_by="test",
    )
    decision_both = Decision(
        question="值得测试吗",
        outcome="小规模测试",
        decision_type="test",
        evidence_ids=[evidence_a.id, evidence_b.id],
        made_by="test",
    )
    decision_unrelated = Decision(
        question="无关决策",
        outcome="保持",
        decision_type="other",
        evidence_ids=[evidence_b.id],
        made_by="test",
    )

    def mutate(item) -> None:
        item.evidence.extend([evidence_a, evidence_b])
        item.decisions.extend([decision_both, decision_unrelated])

    ledger.update_project(project.id, mutate)
    return project, evidence_a, evidence_b, decision_both, decision_unrelated


def test_stale_evidence_flags_linked_decisions_only(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, evidence_a, _b, decision_both, decision_unrelated = seed_project(ledger)

    updated, affected = ledger.update_evidence_status(
        project.id, evidence_a.id, EvidenceStatus.STALE
    )

    assert updated.status == EvidenceStatus.STALE
    assert affected == [decision_both.id]
    stored = ledger.get_project(project.id)
    flagged = next(item for item in stored.decisions if item.id == decision_both.id)
    assert flagged.status == DecisionStatus.NEEDS_REVIEW
    assert flagged.invalidated_by_evidence_ids == [evidence_a.id]
    untouched = next(item for item in stored.decisions if item.id == decision_unrelated.id)
    assert untouched.status == DecisionStatus.ACTIVE
    assert untouched.invalidated_by_evidence_ids == []


def test_restoring_evidence_unflags_when_no_other_invalidation_remains(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, evidence_a, evidence_b, decision_both, _u = seed_project(ledger)

    ledger.update_evidence_status(project.id, evidence_a.id, EvidenceStatus.REMOVED)
    ledger.update_evidence_status(project.id, evidence_b.id, EvidenceStatus.STALE)

    flagged = next(
        item
        for item in ledger.get_project(project.id).decisions
        if item.id == decision_both.id
    )
    assert set(flagged.invalidated_by_evidence_ids) == {evidence_a.id, evidence_b.id}

    # Restoring only A keeps the decision under review because B still invalidates it.
    ledger.update_evidence_status(project.id, evidence_a.id, EvidenceStatus.ACTIVE)
    partially = next(
        item
        for item in ledger.get_project(project.id).decisions
        if item.id == decision_both.id
    )
    assert partially.invalidated_by_evidence_ids == [evidence_b.id]
    assert partially.status == DecisionStatus.NEEDS_REVIEW

    # Restoring B clears the last invalidation and reactivates the decision.
    ledger.update_evidence_status(project.id, evidence_b.id, EvidenceStatus.ACTIVE)
    restored = next(
        item
        for item in ledger.get_project(project.id).decisions
        if item.id == decision_both.id
    )
    assert restored.invalidated_by_evidence_ids == []
    assert restored.status == DecisionStatus.ACTIVE


def test_unknown_evidence_id_raises(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, *_ = seed_project(ledger)
    with pytest.raises(LedgerNotFoundError):
        ledger.update_evidence_status(project.id, "ev_missing", EvidenceStatus.STALE)


def test_search_evidence_filters_by_status_kind_and_text(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, evidence_a, evidence_b, *_ = seed_project(ledger)
    ledger.update_evidence_status(project.id, evidence_b.id, EvidenceStatus.STALE)

    assert {item.id for item in ledger.search_evidence(project.id)} == {
        evidence_a.id,
        evidence_b.id,
    }
    stale_only = ledger.search_evidence(project.id, status=EvidenceStatus.STALE)
    assert [item.id for item in stale_only] == [evidence_b.id]
    facts = ledger.search_evidence(project.id, kind=EvidenceKind.OBSERVED_FACT)
    assert [item.id for item in facts] == [evidence_a.id]
    text = ledger.search_evidence(project.id, query="社媒")
    assert [item.id for item in text] == [evidence_b.id]


class FakeRuntime:
    async def run(self, run) -> None:  # pragma: no cover - not exercised
        return None

    async def cancel(self, run_id: str) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_evidence_endpoints_query_and_invalidate(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    project, evidence_a, _b, decision_both, _u = seed_project(ledger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        listing = await client.get(f"/api/projects/{project.id}/evidence")
        assert listing.status_code == 200
        assert {item["id"] for item in listing.json()} == {evidence_a.id, _b.id}

        filtered = await client.get(
            f"/api/projects/{project.id}/evidence",
            params={"kind": "observed_fact", "q": "6.8"},
        )
        assert [item["id"] for item in filtered.json()] == [evidence_a.id]

        change = await client.post(
            f"/api/projects/{project.id}/evidence/{evidence_a.id}/status",
            json={"status": "stale"},
        )
        assert change.status_code == 200
        body = change.json()
        assert body["evidence"]["status"] == "stale"
        assert body["affected_decision_ids"] == [decision_both.id]

        missing = await client.post(
            f"/api/projects/{project.id}/evidence/ev_missing/status",
            json={"status": "stale"},
        )
        assert missing.status_code == 404
