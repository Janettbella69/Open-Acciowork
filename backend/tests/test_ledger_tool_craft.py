"""教材〔补〕工具手艺回归：默认摘要、错误可自纠、幂等（06 章 v2.2 修正）。"""

import json
from pathlib import Path

import pytest

from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import FileLedger


def handler(tools, name: str):
    return next(item for item in tools if item.name == name).handler


def parse(result: dict):
    return json.loads(result["content"][0]["text"])


def build(tmp_path: Path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    return ledger, project, build_ledger_tools(ledger, project.id)


def evidence_args(claim: str, excerpt: str | None = None) -> dict:
    return {
        "kind": "user_statement",
        "claim": claim,
        "source_title": "constraints",
        "source_type": "user",
        "confidence": 0.9,
        "supports_hypothesis_ids": [],
        "recorded_by": "test",
        **({"excerpt": excerpt} if excerpt else {}),
    }


@pytest.mark.asyncio
async def test_list_views_preview_long_excerpts(tmp_path: Path) -> None:
    _ledger, _project, tools = build(tmp_path)
    long_excerpt = "预算与认证约束" * 300  # 2100 chars
    await handler(tools, "record_evidence")(evidence_args("约束原文", long_excerpt))

    searched = parse(await handler(tools, "search_evidence")({"query": "约束"}))
    assert len(searched) == 1
    assert "preview only" in searched[0]["excerpt"]
    assert len(searched[0]["excerpt"]) < 400

    context = parse(await handler(tools, "read_project_context")({}))
    assert "preview only" in context["evidence"][0]["excerpt"]


@pytest.mark.asyncio
async def test_unknown_reference_errors_teach_the_fix(tmp_path: Path) -> None:
    _ledger, _project, tools = build(tmp_path)
    with pytest.raises(ValueError, match="read_project_context"):
        await handler(tools, "record_evidence")(
            {**evidence_args("x"), "supports_hypothesis_ids": ["hyp_missing"]}
        )
    status = await handler(tools, "update_evidence_status")(
        {"evidence_id": "ev_missing", "status": "stale"}
    )
    assert status["is_error"] is True
    assert "search_evidence" in parse(status)["error"]


@pytest.mark.asyncio
async def test_record_decision_is_idempotent(tmp_path: Path) -> None:
    _ledger, project, tools = build(tmp_path)
    args = {
        "question": "该品类是否值得小规模测试",
        "outcome": "Test：先投 500 件",
        "decision_type": "test",
        "rationale": "证据充分",
        "evidence_ids": [],
        "hypothesis_ids": [],
        "assumptions": ["汇率稳定"],
        "conditions": ["首批合格率>95%"],
        "next_actions": ["下试单"],
        "made_by": "test",
    }
    first = parse(await handler(tools, "record_decision")(args))
    second = parse(await handler(tools, "record_decision")(dict(args)))
    assert first["id"] == second["id"]
