from __future__ import annotations

from pathlib import Path

import pytest

from ophira_backend.config import Settings
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger


def runtime_fixture(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="OCR", objective="Read materials"))
    run = ledger.create_run(project.id, "Analyze")
    events = EventPublisher(ledger)
    runtime = ClaudeAgentRuntime(
        Settings(_env_file=None, data_dir=tmp_path), ledger, events,
        PermissionBroker(ledger, events, timeout_seconds=30),
    )
    workspace = ledger.workspace_for_project(project.id).resolve()
    (workspace / "uploads").mkdir()
    return runtime, run, workspace


@pytest.mark.parametrize("filename", ["quote.pdf", "photo.PNG", "scan.tiff", "clip.gif"])
@pytest.mark.parametrize("absolute", [False, True])
async def test_uploaded_binary_read_points_to_markdown(tmp_path, filename, absolute):
    runtime, run, workspace = runtime_fixture(tmp_path)
    source = workspace / "uploads" / filename
    source.write_bytes(b"untrusted binary fixture")
    source.with_name(filename + ".md").write_text("Converted material")
    candidate = str(source) if absolute else f"uploads/{filename}"
    result = await runtime._hook(run.id, workspace)({
        "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": candidate, "pages": "1-2"},
    }, "read-1", None)
    decision = result["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert f"uploads/{filename}.md" in decision["permissionDecisionReason"]
    assert "missing-page" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("companion_kind", ["missing", "symlink", "directory"])
async def test_missing_or_unsafe_companion_has_actionable_failure(tmp_path, companion_kind):
    runtime, run, workspace = runtime_fixture(tmp_path)
    source = workspace / "uploads" / "quote.pdf"
    source.write_bytes(b"fixture")
    companion = source.with_name("quote.pdf.md")
    if companion_kind == "symlink":
        target = tmp_path / "outside.md"
        target.write_text("Not project content")
        companion.symlink_to(target)
    elif companion_kind == "directory":
        companion.mkdir()
    result = await runtime._hook(run.id, workspace)({
        "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": str(source)},
    }, "read-1", None)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "re-upload" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_upload_binary_read_alias_is_not_a_bypass(tmp_path):
    runtime, run, workspace = runtime_fixture(tmp_path)
    source = workspace / "uploads" / "quote.pdf"
    source.write_bytes(b"fixture")
    alias = workspace / "alias.md"
    alias.symlink_to(source)
    result = await runtime._hook(run.id, workspace)({
        "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": str(alias)},
    }, "read-1", None)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_extensionless_pdf_read_is_also_gated(tmp_path):
    runtime, run, workspace = runtime_fixture(tmp_path)
    source = workspace / "uploads" / "renamed.txt"
    source.write_bytes(b"%PDF-1.7\nfixture")
    result = await runtime._hook(run.id, workspace)({
        "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": str(source)},
    }, "read-1", None)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool, arguments", [
    ("Read", {"file_path": "uploads/quote.pdf.md"}),
    ("Read", {"file_path": "outputs/generated.png"}),
    ("Read", {"file_path": "screenshots/browser.png"}),
    ("Read", {"file_path": "uploads/prices.csv"}),
    ("Bash", {"command": "python resize.py uploads/photo.png outputs/preview.png"}),
])
async def test_markdown_read_and_general_media_processing_remain_available(
    tmp_path, tool, arguments,
):
    runtime, run, workspace = runtime_fixture(tmp_path)
    if "file_path" in arguments:
        file = workspace / arguments["file_path"]
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"fixture")
    result = await runtime._hook(run.id, workspace)({
        "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": arguments,
    }, "tool-1", None)
    assert result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_ingestion_prompt_is_inside_persisted_context_hash(tmp_path: Path):
    import hashlib

    runtime, run, _workspace = runtime_fixture(tmp_path)
    options = runtime._build_options(run)
    prompt = options.system_prompt["append"]
    assert "上传的 PDF/图片必须先经摄取" in prompt
    refresh = runtime.ledger.get_run(run.id).context_refresh
    assert refresh.prompt_prefix_hash == hashlib.sha256(prompt.encode()).hexdigest()
