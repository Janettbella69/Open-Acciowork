"""Phase 1 market MCP extension: search recorded market signals (read-only)."""

from pathlib import Path

from ophira_backend.constants import EvidenceKind, EvidenceSourceType
from ophira_backend.ledger_tools import filter_market_signals
from ophira_backend.models import Evidence, ProjectCreate
from ophira_backend.storage import FileLedger


def seed(ledger: FileLedger):
    project = ledger.create_project(ProjectCreate(name="Signals", objective="Watch"))

    def signal(claim: str, signal_type: str) -> Evidence:
        return Evidence(
            tags=["market-signal", signal_type],
            kind=EvidenceKind.COMMUNITY_SIGNAL,
            claim=claim,
            source_title="source",
            source_url="https://example.org/s",
            source_type=EvidenceSourceType.WEB,
            confidence=0.4,
            recorded_by="ophira-market-mcp",
        )

    plain_evidence = Evidence(
        kind=EvidenceKind.OBSERVED_FACT,
        claim="报价 6.8 USD",
        source_title="quotes.csv",
        source_type=EvidenceSourceType.FILE,
        source_path="uploads/quotes.csv",
        confidence=0.9,
        recorded_by="agent",
    )
    trend = signal("德国露营装备搜索热度上升", "search-trend")
    review = signal("宠物水杯评论区高频求购", "social-buzz")

    def mutate(item) -> None:
        item.evidence.extend([plain_evidence, trend, review])

    ledger.update_project(project.id, mutate)
    return project, trend, review


def test_only_market_signals_are_returned(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, trend, review = seed(ledger)

    results = filter_market_signals(ledger, project.id)

    assert {item.id for item in results} == {trend.id, review.id}


def test_signal_type_and_query_filters(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, trend, review = seed(ledger)

    by_type = filter_market_signals(ledger, project.id, signal_type="search-trend")
    assert [item.id for item in by_type] == [trend.id]

    by_query = filter_market_signals(ledger, project.id, query="宠物")
    assert [item.id for item in by_query] == [review.id]

    assert filter_market_signals(ledger, project.id, signal_type="nonexistent") == []


def test_limit_applies_after_signal_type_filter(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, _trend, _review = seed(ledger)

    limited = filter_market_signals(ledger, project.id, limit=1)
    assert len(limited) == 1
