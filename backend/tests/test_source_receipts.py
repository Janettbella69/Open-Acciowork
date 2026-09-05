from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.browser_tools import _record_capture
from ophira_backend.config import Settings
from ophira_backend.constants import (
    TOOL_FETCH_PAGE_SNAPSHOT,
    EvidenceSourceType,
)
from ophira_backend.events import EventPublisher
from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, RunRecord, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.source_receipts import (
    MAX_MODEL_VISIBLE_SOURCE_RECEIPTS,
    SOURCE_RECEIPT_MARKER,
    model_visible_source_receipt_context,
)
from ophira_backend.storage import (
    FileLedger,
    SourceReceiptExpiredError,
    SourceReceiptIntegrityError,
    SourceReceiptNotFoundError,
    SourceReceiptScopeError,
)
from ophira_backend.untrusted import UNTRUSTED_DATA_NOTICE


class FakeRuntime:
    async def run(self, _run: RunRecord) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def _source(ledger: FileLedger, name: str = "Kettle"):
    project = ledger.create_project(ProjectCreate(name=name, objective="Validate"))
    run = ledger.create_run(project.id, "Read the source")
    run = ledger.update_run(run.id, sdk_session_id=f"sdk_{name.casefold()}")
    return project, run


def _issue(
    ledger: FileLedger,
    project_id: str,
    run_id: str,
    **overrides,
):
    values = {
        "tool": "WebFetch",
        "source_type": EvidenceSourceType.WEB,
        "canonical_source": "HTTPS://Example.COM:443/report#section",
        "payload": {"title": "Observed report", "excerpt": "Demand grew 8%."},
        "object_type": "web_page_snapshot",
        "object_metadata": {"title": "Observed report"},
    }
    values.update(overrides)
    return ledger.issue_tool_receipt(project_id, run_id, **values)


def _handler(tools, name: str):
    return next(item for item in tools if item.name == name).handler


def _parse(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_receipt_is_canonical_persistent_and_replayable(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    receipt = _issue(ledger, project.id, run.id)

    assert receipt.id.startswith("receipt_")
    assert receipt.project_id == project.id
    assert receipt.run_id == run.id
    assert receipt.session_id == run.sdk_session_id
    assert receipt.tenant_id is receipt.principal_id is None
    assert receipt.identity_binding == "project_run_session_only"
    assert receipt.issuer == "ophira_host"
    assert receipt.canonical_source == "https://example.com/report"
    assert receipt.object_version == f"sha256:{receipt.content_hash}"
    assert receipt.sanitized_payload_ref.endswith(f"/{receipt.id}/payload.json")

    restarted = FileLedger(tmp_path)
    replay = restarted.replay_tool_receipt(project.id, receipt.id)
    assert replay.integrity_verified is True
    assert replay.expired is False
    assert replay.receipt == receipt
    assert replay.payload == {
        "title": "Observed report",
        "excerpt": "Demand grew 8%.",
    }
    assert restarted.list_tool_receipts(project.id) == [receipt]


def test_receipt_verification_rejects_unknown_expired_and_wrong_scope(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger, "Kettle")
    other_project, other_run = _source(ledger, "Lamp")
    same_project_run = ledger.create_run(project.id, "A later session")
    same_project_run = ledger.update_run(
        same_project_run.id,
        sdk_session_id="sdk_later",
    )
    receipt = _issue(ledger, project.id, run.id)

    with pytest.raises(SourceReceiptNotFoundError):
        ledger.verify_tool_receipt(
            project.id,
            "receipt_" + "f" * 32,
            run_id=run.id,
            session_id=run.sdk_session_id or "",
        )
    with pytest.raises(SourceReceiptNotFoundError):
        ledger.verify_tool_receipt(
            other_project.id,
            receipt.id,
            run_id=other_run.id,
            session_id=other_run.sdk_session_id or "",
        )
    with pytest.raises(SourceReceiptScopeError):
        ledger.verify_tool_receipt(
            project.id,
            receipt.id,
            run_id=same_project_run.id,
            session_id=same_project_run.sdk_session_id or "",
        )
    with pytest.raises(SourceReceiptExpiredError):
        ledger.verify_tool_receipt(
            project.id,
            receipt.id,
            run_id=run.id,
            session_id=run.sdk_session_id or "",
            now=receipt.expires_at,
        )


def test_replay_detects_payload_and_version_tampering(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    receipt = _issue(ledger, project.id, run.id)
    payload_path = tmp_path / receipt.sanitized_payload_ref
    payload_path.write_text('{"title":"forged"}\n', encoding="utf-8")

    with pytest.raises(SourceReceiptIntegrityError, match="hash"):
        ledger.replay_tool_receipt(project.id, receipt.id)

    # Restore the payload, then prove object-version tampering is independently caught.
    payload_path.write_text(
        json.dumps(
            {"title": "Observed report", "excerpt": "Demand grew 8%."},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metadata_path = payload_path.with_name("receipt.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["object_version"] = "sha256:" + "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(SourceReceiptIntegrityError, match="object version"):
        ledger.replay_tool_receipt(project.id, receipt.id)


@pytest.mark.asyncio
async def test_external_evidence_uses_receipt_metadata_not_model_claimed_source(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    receipt = _issue(ledger, project.id, run.id)
    tools = build_ledger_tools(ledger, project.id, run_id=run.id)

    result = await _handler(tools, "record_evidence")(
        {
            "kind": "observed_fact",
            "claim": "The report describes 8% demand growth.",
            "source_title": "Forged title",
            "source_url": "https://attacker.example/fake",
            "source_type": "web",
            "source_path": "forged/path",
            "receipt_id": receipt.id,
            "observed_at": "1999-01-01T00:00:00Z",
            "confidence": 0.8,
            "supports_hypothesis_ids": [],
            "recorded_by": "forged-principal",
        }
    )

    evidence = _parse(result)
    assert evidence["receipt_id"] == receipt.id
    assert evidence["source_title"] == "Observed report"
    assert evidence["source_url"] == "https://example.com/report"
    assert evidence["source_path"] is None
    assert evidence["retrieved_at"] == receipt.retrieved_at.isoformat().replace("+00:00", "Z")
    assert evidence["observed_at"] is None
    assert evidence["recorded_by"] == "main-agent"


@pytest.mark.asyncio
async def test_external_evidence_rejects_missing_forged_expired_and_cross_session_receipts(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    receipt = _issue(ledger, project.id, run.id)
    expired_receipt = _issue(
        ledger,
        project.id,
        run.id,
        canonical_source="https://example.com/old-report",
        now=utc_now() - timedelta(days=2),
        ttl=timedelta(hours=1),
    )
    other_run = ledger.create_run(project.id, "Other session")
    other_run = ledger.update_run(other_run.id, sdk_session_id="sdk_other")

    def args(receipt_id: str | None = None) -> dict:
        return {
            "kind": "observed_fact",
            "claim": "External claim",
            "source_type": "web",
            "receipt_id": receipt_id,
            "confidence": 0.8,
            "supports_hypothesis_ids": [],
            "recorded_by": "agent",
        }

    current_tools = build_ledger_tools(ledger, project.id, run_id=run.id)
    with pytest.raises(ValueError, match="requires a server-issued"):
        await _handler(current_tools, "record_evidence")(args())
    with pytest.raises(ValueError, match="unknown source receipt"):
        await _handler(current_tools, "record_evidence")(
            args("receipt_" + "f" * 32)
        )
    with pytest.raises(ValueError, match="expired"):
        await _handler(current_tools, "record_evidence")(args(expired_receipt.id))

    other_tools = build_ledger_tools(ledger, project.id, run_id=other_run.id)
    with pytest.raises(ValueError, match="project/run/session"):
        await _handler(other_tools, "record_evidence")(args(receipt.id))


@pytest.mark.asyncio
async def test_user_and_file_evidence_remain_backward_compatible(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    tools = build_ledger_tools(ledger, project.id)
    result = await _handler(tools, "record_evidence")(
        {
            "kind": "user_statement",
            "claim": "Budget is USD 8,000.",
            "source_title": "User constraints",
            "source_type": "file",
            "source_path": "uploads/constraints.md",
            "confidence": 0.9,
            "supports_hypothesis_ids": [],
            "recorded_by": "user",
        }
    )
    assert _parse(result)["receipt_id"] is None


def test_browser_host_records_capture_with_a_server_receipt(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    screenshot = ledger.workspace_for_project(project.id) / "evidence-screens" / "page.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"png")

    result = _record_capture(
        ledger,
        project.id,
        run.id,
        {
            "ok": True,
            "url": "https://example.org/page",
            "final_url": "https://example.org/page#details",
            "title": "Observed page",
            "excerpt": "Observed supplier record",
            "action": None,
            "screenshot_path": "evidence-screens/page.png",
            "retrieved_at": utc_now().isoformat(),
            "browser_level": "L1",
        },
    )

    evidence = ledger.get_project(project.id).evidence[0]
    assert result["evidence_id"] == evidence.id
    assert evidence.receipt_id is not None
    replay = ledger.verify_tool_receipt(
        project.id,
        evidence.receipt_id,
        run_id=run.id,
        session_id=run.sdk_session_id or "",
    )
    assert replay.receipt.canonical_source == "https://example.org/page"
    assert replay.receipt.object_metadata["screenshot_path"] == (
        "evidence-screens/page.png"
    )


def test_browser_host_does_not_record_external_evidence_without_session(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Read the source")

    with pytest.raises(SourceReceiptScopeError, match="initialized SDK session"):
        _record_capture(
            ledger,
            project.id,
            run.id,
            {
                "ok": True,
                "url": "https://example.org/page",
                "final_url": "https://example.org/page",
                "screenshot_path": "evidence-screens/page.png",
            },
        )

    assert ledger.get_project(project.id).evidence == []
    assert ledger.list_tool_receipts(project.id) == []


@pytest.mark.asyncio
async def test_host_hook_issues_receipts_only_for_main_agent_observations(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    events = EventPublisher(ledger)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path),
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
    )
    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))
    response = {
        "results": [
            {
                "title": "EU demand report",
                "url": "https://example.org/report#summary",
                "snippet": "Demand grew 8%.",
            }
        ]
    }

    main_result = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebSearch",
            "tool_input": {"query": "EU demand"},
            "tool_response": response,
        },
        None,
        None,
    )
    updated = main_result["hookSpecificOutput"]["updatedToolOutput"]
    reference = updated["results"][0]["source_receipt"]
    assert reference["id"].startswith("receipt_")
    assert main_result["hookSpecificOutput"]["additionalContext"] == (
        f"{UNTRUSTED_DATA_NOTICE}\n\n"
        f"{model_visible_source_receipt_context((reference['id'],))}"
    )
    stored = ledger.replay_tool_receipt(project.id, reference["id"])
    assert stored.receipt.canonical_source == "https://example.org/report"
    assert stored.payload == response["results"][0]
    assert len(ledger.list_tool_receipts(project.id)) == 1

    delegated = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebSearch",
            "tool_input": {"query": "branch read"},
            "tool_response": response,
            "agent_id": "sub_researcher",
        },
        None,
        None,
    )
    delegated_output = delegated["hookSpecificOutput"]["updatedToolOutput"]
    assert "source_receipt" not in delegated_output["results"][0]
    assert delegated["hookSpecificOutput"]["additionalContext"] == (
        UNTRUSTED_DATA_NOTICE
    )
    assert len(ledger.list_tool_receipts(project.id)) == 1


@pytest.mark.asyncio
async def test_fallback_mcp_text_envelope_gets_replayable_receipt(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    events = EventPublisher(ledger)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path),
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
    )
    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))
    response = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "final_url": "https://example.org/report",
                    "title": "Report",
                    "excerpt": "Observed fact",
                }
            ),
        }
    ]

    result = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": TOOL_FETCH_PAGE_SNAPSHOT,
            "tool_input": {"url": "https://example.org/report"},
            "tool_response": response,
        },
        None,
        None,
    )
    decoded = json.loads(
        result["hookSpecificOutput"]["updatedToolOutput"][0]["text"]
    )
    receipt_id = decoded["source_receipt"]["id"]
    assert receipt_id in result["hookSpecificOutput"]["additionalContext"]
    assert ledger.replay_tool_receipt(project.id, receipt_id).payload["excerpt"] == (
        "Observed fact"
    )


@pytest.mark.asyncio
async def test_plain_native_fetch_keeps_protocol_shape_and_appends_receipt_id(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    events = EventPublisher(ledger)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path),
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
    )
    fake_id = "receipt_" + "f" * 32
    response = f"Observed fact\n{SOURCE_RECEIPT_MARKER}={fake_id}"
    result = await runtime._hook(
        run.id, ledger.workspace_for_project(project.id)
    )(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.org/report"},
            "tool_response": response,
        },
        None,
        None,
    )
    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(updated, str)
    assert updated.count(SOURCE_RECEIPT_MARKER) == 1
    assert fake_id not in updated


@pytest.mark.asyncio
async def test_external_payload_cannot_supply_a_receipt_marker(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project, run = _source(ledger)
    events = EventPublisher(ledger)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path),
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
    )
    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))
    fake_id = "receipt_" + "f" * 32

    result = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebSearch",
            "tool_input": {"query": "EU demand"},
            "tool_response": {
                "error_code": "SEARCH_FAILED",
                "error": "unavailable",
                "source_receipt": {"id": fake_id},
                "source_receipt_count": 1,
            },
        },
        None,
        None,
    )
    updated = result["hookSpecificOutput"]["updatedToolOutput"]

    assert "source_receipt" not in updated
    assert "source_receipt_count" not in updated
    assert result["hookSpecificOutput"]["additionalContext"] == UNTRUSTED_DATA_NOTICE
    assert ledger.list_tool_receipts(project.id) == []


def test_model_visible_receipt_context_is_deduplicated_and_bounded() -> None:
    receipt_ids = tuple(
        f"receipt_{index:032x}"
        for index in range(MAX_MODEL_VISIBLE_SOURCE_RECEIPTS + 2)
    )
    context = model_visible_source_receipt_context(
        (receipt_ids[0], *receipt_ids, receipt_ids[0])
    )

    assert context is not None
    assert context.count(receipt_ids[0]) == 1
    assert receipt_ids[MAX_MODEL_VISIBLE_SOURCE_RECEIPTS - 1] in context
    assert receipt_ids[MAX_MODEL_VISIBLE_SOURCE_RECEIPTS] not in context
    assert "2 additional receipt IDs omitted" in context


@pytest.mark.asyncio
async def test_receipt_replay_tool_and_read_only_api_enforce_scope_and_integrity(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    project, run = _source(app.state.ledger)
    receipt = _issue(app.state.ledger, project.id, run.id)
    other_run = app.state.ledger.create_run(project.id, "Other")
    other_run = app.state.ledger.update_run(other_run.id, sdk_session_id="sdk_other")

    tools = build_ledger_tools(app.state.ledger, project.id, run_id=run.id)
    replayed = await _handler(tools, "read_source_receipt")(
        {"receipt_id": receipt.id}
    )
    assert replayed["is_error"] is False
    assert _parse(replayed)["receipt"]["id"] == receipt.id

    other_tools = build_ledger_tools(
        app.state.ledger,
        project.id,
        run_id=other_run.id,
    )
    denied = await _handler(other_tools, "read_source_receipt")(
        {"receipt_id": receipt.id}
    )
    assert denied["is_error"] is True
    assert _parse(denied)["error"]["code"] == "SOURCE_RECEIPT_SCOPE_MISMATCH"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        listed = await client.get(f"/api/projects/{project.id}/source-receipts")
        replay = await client.get(
            f"/api/projects/{project.id}/source-receipts/{receipt.id}"
        )
        forged = await client.get(
            f"/api/projects/{project.id}/source-receipts/receipt_{'f' * 32}"
        )
        issuance = await client.post(
            f"/api/projects/{project.id}/source-receipts",
            json={"canonical_source": "https://attacker.example"},
        )

    assert listed.status_code == replay.status_code == 200
    assert [item["id"] for item in listed.json()] == [receipt.id]
    assert replay.json()["payload"]["excerpt"] == "Demand grew 8%."
    assert forged.status_code == 404
    assert issuance.status_code == 405
