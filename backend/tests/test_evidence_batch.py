"""FM-02 improvement: batch evidence recording in one authorized tool call."""

import json
from pathlib import Path

import pytest

from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import FileLedger


def handler(tools, name: str):
    tool = next(item for item in tools if item.name == name)
    return tool.handler


def entry(claim: str, **overrides) -> dict:
    payload = {
        "kind": "user_statement",
        "claim": claim,
        "source_title": "材料",
        "source_type": "file",
        "source_path": "uploads/notes.md",
        "confidence": 0.8,
        "supports_hypothesis_ids": [],
        "recorded_by": "test-agent",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_batch_records_multiple_entries_in_one_call(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Batch", objective="Record"))
    tools = build_ledger_tools(ledger, project.id)

    result = await handler(tools, "record_evidence_batch")(
        {"entries": [entry("产能月产 12 万件"), entry("认证 LFGB+FDA"), entry("MOQ 可谈至 1000")]}
    )

    assert result["is_error"] is False
    returned = json.loads(result["content"][0]["text"])
    assert len(returned) == 3
    assert len(ledger.get_project(project.id).evidence) == 3


@pytest.mark.asyncio
async def test_batch_is_idempotent_per_entry(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Batch", objective="Record"))
    tools = build_ledger_tools(ledger, project.id)

    await handler(tools, "record_evidence")(entry("产能月产 12 万件"))
    result = await handler(tools, "record_evidence_batch")(
        {"entries": [entry("产能月产 12 万件"), entry("新事实")]}
    )

    returned = json.loads(result["content"][0]["text"])
    assert len(returned) == 2
    # Duplicate merged onto the existing record; only one new evidence row appended.
    assert len(ledger.get_project(project.id).evidence) == 2


@pytest.mark.asyncio
async def test_batch_rejects_unknown_hypothesis_links(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Batch", objective="Record"))
    tools = build_ledger_tools(ledger, project.id)

    with pytest.raises(ValueError, match="unknown hypothesis ids"):
        await handler(tools, "record_evidence_batch")(
            {"entries": [entry("x", supports_hypothesis_ids=["hyp_missing"])]}
        )
