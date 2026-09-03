import asyncio
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.channels import feishu_challenge, verify_feishu_signature
from ophira_backend.config import Settings
from ophira_backend.constants import (
    HEARTBEAT_OK,
    EvidenceKind,
    EvidenceSourceType,
    MemorySource,
    PermissionDecision,
    ScheduleKind,
)
from ophira_backend.destructive import is_destructive_delete
from ophira_backend.distill import distill_run
from ophira_backend.events import EventPublisher
from ophira_backend.grants import PermissionGrantStore
from ophira_backend.ingest import needs_markdown_companion
from ophira_backend.main import create_app
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import (
    Decision,
    Evidence,
    PermissionResolution,
    ProjectCreate,
    ScheduleCreate,
    utc_now,
)
from ophira_backend.office_export import write_xlsx
from ophira_backend.permissions import PermissionBroker
from ophira_backend.scheduler import wrap_schedule_prompt
from ophira_backend.storage import FileLedger


class FakeRuntime:
    async def run(self, run) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


def test_rm_and_delete_are_destructive() -> None:
    assert is_destructive_delete("Delete", {"path": "a.md"})
    assert is_destructive_delete("Bash", {"command": "rm -rf workspace/out"})
    assert not is_destructive_delete("Read", {"file_path": "a.md"})
    assert not is_destructive_delete("Bash", {"command": "ls workspace"})


@pytest.mark.asyncio
async def test_persistent_grant_skips_repeat_prompt(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    grants = PermissionGrantStore(tmp_path / "grants")
    broker = PermissionBroker(ledger, events, timeout_seconds=30, grants=grants)

    pending = asyncio.create_task(
        broker.request(
            run.id,
            "WebFetch",
            {"url": "https://example.com"},
            agent_id=None,
            context={},
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if broker.list_pending():
            break
    request_id = broker.list_pending()[0].id
    await broker.resolve(
        run.id,
        request_id,
        PermissionResolution(decision=PermissionDecision.ALLOW_PERSISTENT),
    )
    first = await pending
    assert first.decision == PermissionDecision.ALLOW_PERSISTENT

    remembered = await broker.request(
        run.id, "WebFetch", {"url": "https://example.com/page"}, agent_id=None, context={}
    )
    assert remembered.decision == PermissionDecision.ALLOW_PERSISTENT


@pytest.mark.asyncio
async def test_destructive_delete_is_never_covered_by_run_allowlist(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    broker = PermissionBroker(ledger, events, timeout_seconds=30)
    first = asyncio.create_task(
        broker.request(run.id, "Delete", {"path": "a.md"}, agent_id=None, context={})
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if broker.list_pending():
            break
    await broker.resolve(
        run.id,
        broker.list_pending()[0].id,
        PermissionResolution(decision=PermissionDecision.ALLOW_RUN),
    )
    await first
    second = asyncio.create_task(
        broker.request(run.id, "Delete", {"path": "b.md"}, agent_id=None, context={})
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if any(item.tool_name == "Delete" for item in broker.list_pending()):
            break
    assert broker.list_pending()
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)


def test_distill_only_proposes_trusted_project_truth(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    memory = FileMemoryProvider(tmp_path / "memory")
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")

    def mutate(item) -> None:
        item.decisions.append(
            Decision(
                question="进入欧盟？",
                outcome="先小单测试",
                made_by="user",
                source_run_id=run.id,
            )
        )
        item.decisions.append(
            Decision(
                question="旧运行的结论？",
                outcome="不应再次蒸馏",
                made_by="user",
                source_run_id="run_previous",
            )
        )
        item.evidence.append(
            Evidence(
                kind=EvidenceKind.USER_STATEMENT,
                claim="预算不超过 8000 USD",
                source_title="用户",
                source_type=EvidenceSourceType.USER,
                confidence=0.9,
                recorded_by="user",
                source_run_id=run.id,
            )
        )
        item.evidence.append(
            Evidence(
                kind=EvidenceKind.COMMUNITY_SIGNAL,
                claim="某论坛说很好卖",
                source_title="forum",
                source_type=EvidenceSourceType.WEB,
                confidence=0.2,
                recorded_by="agent",
                source_run_id=run.id,
            )
        )

    ledger.update_project(project.id, mutate)
    proposed = distill_run(ledger, memory, run)
    texts = [item.text for item in proposed]
    assert any("进入欧盟" in text for text in texts)
    assert any("8000" in text for text in texts)
    assert all(item.source == MemorySource.DISTILLATION for item in proposed)
    assert all("很好卖" not in text for text in texts)
    assert all("旧运行" not in text for text in texts)


def test_upload_writes_markdown_companion(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    assert needs_markdown_companion("brief.pdf")
    ledger.save_upload(project.id, "brief.pdf", b"%PDF-1.1\ntrailer\n%%EOF\n")
    project = ledger.get_project(project.id)
    companion = next(item for item in project.artifacts if item.path.endswith(".pdf.md"))
    text = (ledger.workspace_for_project(project.id) / companion.path).read_text("utf-8")
    assert "不可信" in text
    assert "brief.pdf" in text


def test_xlsx_contains_a_real_formula(tmp_path: Path) -> None:
    path = tmp_path / "sheet.xlsx"
    write_xlsx(path, ["item", "value"], [["a", "1"], ["b", "2"]])
    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml")
    assert b"<f>SUM(" in sheet


def test_heartbeat_prompt_wraps_and_feishu_challenge() -> None:
    schedule = ScheduleCreate(
        project_id="p",
        prompt="check prices",
        cron="0 * * * *",
        kind=ScheduleKind.HEARTBEAT,
    )
    from ophira_backend.scheduler import new_schedule

    wrapped = wrap_schedule_prompt(new_schedule(schedule))
    assert HEARTBEAT_OK in wrapped
    assert "check prices" in wrapped
    challenge = feishu_challenge({"type": "url_verification", "challenge": "abc"})
    assert challenge == {"challenge": "abc"}
    assert verify_feishu_signature("1", "nonce", "sig", "", b"{}") is False


@pytest.mark.asyncio
async def test_org_policy_and_inbox_apis(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/projects",
            json={"name": "Kettle", "objective": "Validate"},
        )
        project_id = created.json()["id"]
        policy = await client.put(
            "/api/org/policy",
            json={
                "permission_mode": "auto",
                "allowed_provider_ids": [],
                "allowed_skill_ids": [],
                "browser_enabled": True,
                "computer_control_enabled": False,
                "channel_delivery_enabled": False,
                "domain_allowlist": [],
                "updated_at": utc_now().isoformat(),
            },
        )
        assert policy.status_code == 200
        assert policy.json()["permission_mode"] == "auto"

        constrained = await client.post(
            f"/api/projects/{project_id}/constraints",
            json={"constraints": ["只做欧盟"]},
        )
        assert constrained.status_code == 200
        assert "只做欧盟" in constrained.json()["constraints"]

        feedback = await client.post(
            "/api/feedback",
            json={"kind": "conclusion_wrong", "user_note": "结论过宽", "project_id": project_id},
        )
        assert feedback.status_code == 201
        listed = await client.get("/api/feedback")
        assert listed.json()[0]["user_note"] == "结论过宽"

        challenge = await client.post(
            "/api/channels/feishu",
            json={"type": "url_verification", "challenge": "token-1"},
        )
        assert challenge.status_code == 503

        pending = await client.get("/api/permissions/pending")
        assert pending.status_code == 200
        assert pending.json() == []
