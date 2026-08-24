"""Market signals live in the ledger evidence domain (v2.2 merge: no separate MCP).

市场信号=带 market-signal 标签的证据；专用 server 已按教材"相同域合并"删除，
这里回归的是通用 search_evidence 通道对同等场景的覆盖。
"""

from pathlib import Path

from ophira_backend.constants import EvidenceKind, EvidenceSourceType
from ophira_backend.models import Evidence, ProjectCreate
from ophira_backend.storage import FileLedger


def record_signal(ledger: FileLedger, project_id: str, claim: str, signal_type: str) -> None:
    evidence = Evidence(
        tags=["market-signal", signal_type],
        kind=EvidenceKind.COMMUNITY_SIGNAL,
        claim=claim,
        source_title="signal source",
        source_url=f"https://example.com/{signal_type}",
        source_type=EvidenceSourceType.WEB,
        confidence=0.4,
        recorded_by="test",
    )
    ledger.update_project(project_id, lambda project: project.evidence.append(evidence))


def test_market_signals_are_searchable_by_tag(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Blender", objective="Validate"))
    record_signal(ledger, project.id, "DE 站便携榨汁杯热度上升", "search-trend")
    record_signal(ledger, project.id, "TikTok 出现同类爆款视频", "social-buzz")
    ledger.update_project(
        project.id,
        lambda p: p.evidence.append(
            Evidence(
                kind=EvidenceKind.USER_STATEMENT,
                claim="预算上限 5000 USD",
                source_title="constraints",
                source_type=EvidenceSourceType.USER,
                confidence=0.9,
                recorded_by="test",
            )
        ),
    )

    all_signals = ledger.search_evidence(project.id, query="", tag="market-signal", limit=20)
    assert {item.claim for item in all_signals} == {
        "DE 站便携榨汁杯热度上升",
        "TikTok 出现同类爆款视频",
    }

    by_type = ledger.search_evidence(project.id, query="", tag="social-buzz", limit=20)
    assert [item.claim for item in by_type] == ["TikTok 出现同类爆款视频"]
