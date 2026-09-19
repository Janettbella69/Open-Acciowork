from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import timedelta
from uuid import uuid4

import pytest

from ophira_backend import project_export_capture
from ophira_backend.auth import ProjectAccessStore
from ophira_backend.channel_delivery import ChannelDeliveryStore, FeishuReplyTarget
from ophira_backend.config import Settings
from ophira_backend.constants import (
    EventType,
    EvidenceSourceType,
    FeedbackKind,
    MemoryKind,
    MemoryScope,
    MemorySource,
    RunStatus,
)
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import (
    EventActor,
    EventEnvelope,
    FeedbackItem,
    Notification,
    PermissionGrant,
    ProjectCreate,
    Schedule,
    StandingIntent,
    TaskExperienceLedger,
    utc_now,
)
from ophira_backend.project_comments import CommentCreate, ProjectComments
from ophira_backend.project_export_models import ProjectExportConflict
from ophira_backend.project_exports import ProjectExports
from ophira_backend.project_template_models import ProjectSeed, TemplateSave
from ophira_backend.project_templates import ProjectTemplates
from ophira_backend.shop_browser_store import ShopIdentity
from ophira_backend.storage import FileLedger
from ophira_backend.workspace_checkpoints import workspace_lease


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "data"
    ledger = FileLedger(root)
    access = ProjectAccessStore(root / "auth/projects")
    settings = Settings(_env_file=None, data_dir=root)
    service = ProjectExports(ledger, access, settings)
    project = ledger.create_project(ProjectCreate(name="Export", objective="Retain our work"))
    return service, project


def finished_run(ledger, project_id):
    run = ledger.create_run(project_id, "Review supplier terms")
    return ledger.update_run(
        run.id, sdk_session_id="sdk-private-session-canary", status=RunStatus.COMPLETED,
    )


def unpack(service, project_id):
    preview = service.preview(project_id, None)
    archive = service.download(project_id, None, preview.fingerprint)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        assert zipped.testzip() is None
        return preview, {name: zipped.read(name) for name in zipped.namelist()}


def test_archive_preserves_workspace_versions_conversation_sources_and_excludes_shared_data(
    source, monkeypatch,
):
    service, project = source
    ledger = service.ledger
    old = ledger.save_upload(project.id, "quote.txt", b"original supplier quote")
    workspace = ledger.workspace_for_project(project.id)
    (workspace / old.path).write_bytes(b"revised supplier quote")
    new = ledger.register_artifact(project.id, old.path)
    # Unregistered work and empty folders are also user-owned project data.
    (workspace / "scratch.bin").write_bytes(b"\x00\xff\x80original bytes")
    (workspace / "empty").mkdir()
    run = finished_run(ledger, project.id)
    long_text = "完整供应商对话" * 4000
    ledger.append_event(EventEnvelope(
        id="pending", sequence=1, type=EventType.ASSISTANT_MESSAGE_COMPLETED,
        project_id=project.id, run_id=run.id, trace_id=run.trace_id,
        actor=EventActor(type="assistant", name="Main"),
        data={"text": long_text, "password": "secret-canary", "input_tokens": 123,
              "requested_session_id": "login-session-canary"},
    ))
    receipt = ledger.issue_tool_receipt(
        project.id, run.id, tool="WebFetch", source_type=EvidenceSourceType.WEB,
        canonical_source="https://example.com/report", payload={"quote": "Observed quotation"},
        object_type="web_page_snapshot",
    )
    other = ledger.create_project(ProjectCreate(name="Other", objective="private-project-canary"))
    finished_run(ledger, other.id)
    ledger.save_upload(other.id, "private.txt", b"private-project-canary")
    for relative in (
        "secrets/fixture.json", "runtime-config/profile/transcript.json", "agents/shared/diary.md",
        f"runs/{run.id}/native-transcript.json", "shop-browsers/shop_test/profile/Cookies",
    ):
        path = ledger.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"excluded-state-canary")
    read = project_export_capture.read_relative

    def forbid_credentials(root, relative, limit):
        assert not relative.startswith(("secrets/", "runtime-config/", "agents/"))
        return read(root, relative, limit)

    monkeypatch.setattr(project_export_capture, "read_relative", forbid_credentials)
    preview, files = unpack(service, project.id)
    assert files["workspace/scratch.bin"] == b"\x00\xff\x80original bytes"
    assert files["workspace/empty/"] == b""
    assert files[f"artifact-versions/{old.checksum}"] == b"original supplier quote"
    assert files[f"artifact-versions/{new.checksum}"] == b"revised supplier quote"
    message = json.loads(files[f"runs/{run.id}/events.jsonl"])
    assert message["data"]["text"] == long_text
    assert message["data"]["input_tokens"] == 123
    assert json.loads(files[f"receipts/{receipt.id}/payload.json"])["quote"] == "Observed quotation"
    joined = b"".join(files.values())
    for canary in (
        b"private-project-canary", b"excluded-state-canary", b"secret-canary",
        b"sdk-private-session-canary", b"login-session-canary",
    ):
        assert canary not in joined
    manifest = json.loads(files["manifest.json"])
    assert manifest["fingerprint"] == preview.fingerprint
    assert len(preview.entries) == preview.file_count
    assert sum(item.size_bytes for item in preview.entries) == preview.total_bytes
    for item in preview.entries:
        assert hashlib.sha256(files[item.path]).hexdigest() == item.sha256
    assert not (ledger.root / "exports").exists()


def test_memory_tombstones_exclude_deleted_content_even_before_scrub_recovery(source):
    service, project = source
    memory = FileMemoryProvider(service.ledger.root / "memory")

    def create(text, **extra):
        return memory.propose(
            scope=MemoryScope.PROJECT, scope_id=project.id, text=text,
            kind=MemoryKind.FACT, source=MemorySource.USER, **extra,
        )

    live = create("retained project memory")
    deleted = create("deleted-memory-canary")
    directory = memory.root / "project" / project.id
    items_before = (directory / "items.json").read_bytes()
    history_before = (directory / "history.jsonl").read_bytes()
    memory.delete(deleted.id, deleted_by="local-operator", reason="Remove this fact")
    # Simulate the documented crash window after tombstone fsync, before scrubbing.
    (directory / "items.json").write_bytes(items_before)
    (directory / "history.jsonl").write_bytes(history_before)
    memory.propose(
        scope=MemoryScope.AGENT, scope_id="shared-agent", text="shared-memory-canary",
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )
    _, files = unpack(service, project.id)
    assert [item["id"] for item in json.loads(files["memory/items.json"])] == [live.id]
    assert b"deleted-memory-canary" not in b"".join(files.values())
    assert b"shared-memory-canary" not in b"".join(files.values())
    assert b"deleted-memory-canary" in (directory / "items.json").read_bytes()


def test_saved_template_references_export_without_private_creation_requests(source):
    service, project = source
    artifact = service.ledger.save_upload(project.id, "source.txt", b"frozen template source")
    templates = ProjectTemplates(service.ledger, service.access, service.settings)
    template = templates.save(project.id, TemplateSave(
        request_id=uuid4().hex, title="Saved brief", artifact_ids=[artifact.id],
        seed=ProjectSeed(name="New project", objective="Reuse the reviewed brief"),
    ), None)
    folder = templates.folder(project.id, template["id"])
    private = folder / "applications" / "other-user.json"
    private.parent.mkdir()
    private.write_text('{"request":"another-users-private-request"}')
    _, files = unpack(service, project.id)
    prefix = f"templates/{template['id']}"
    assert files[prefix + f"/artifact-versions/{artifact.checksum}"] == b"frozen template source"
    assert json.loads(files[prefix + "/template.json"])["versions"][0]["title"] == "Saved brief"
    assert b"another-users-private-request" not in b"".join(files.values())


def test_archive_includes_project_automation_feedback_access_discussion_and_delivery(source):
    service, project = source
    ledger = service.ledger
    run = finished_run(ledger, project.id)
    schedule = ledger.create_schedule(Schedule(
        project_id=project.id, prompt="Check supplier quotes", cron="0 9 * * *",
    ))
    experience = TaskExperienceLedger(project_id=project.id, schedule_id=schedule.id)
    ledger._atomic_write(ledger.root / "task-experiences" / f"{schedule.id}.json", experience)
    records = [
        ("intents", StandingIntent(
            project_id=project.id, intent="Notify when the supplier replies",
            expires_at=utc_now() + timedelta(days=1),
        )),
        ("notifications", Notification(project_id=project.id, run_id=run.id, title="Review ready")),
        ("grants", PermissionGrant(
            project_id=project.id, tool_name="WebFetch", expires_at=utc_now() + timedelta(days=1),
        )),
        ("feedback", FeedbackItem(
            project_id=project.id, run_id=run.id, kind=FeedbackKind.EVIDENCE_ISSUE,
            user_note="Needs another source",
        )),
        ("shop-browsers", ShopIdentity(
            name="Project shop", platform="shopify", seller_shop_id="our-store",
            entry_url="https://admin.shopify.com/store/our-store", project_id=project.id,
            tenant_id=None, identity_binding="local_only", created_by="local-operator",
        )),
    ]
    for root, record in records:
        ledger._atomic_write(ledger.root / root / f"{record.id}.json", record)
    comments = ProjectComments(ledger, service.access, service.settings)
    comments.create(project.id, CommentCreate(
        request_id=uuid4().hex, text="Discuss the current supplier quote",
    ), None)
    delivery = ChannelDeliveryStore(ledger.root / "channel-deliveries").bind_run(
        run, FeishuReplyTarget(event_id="test-event", message_id="om_reply", chat_id="oc_project"),
        max_attempts=3,
    )
    preview, files = unpack(service, project.id)
    for root, record in records:
        assert json.loads(files[f"{root}/{record.id}.json"])["id"] == record.id
    assert json.loads(files[f"schedules/{schedule.id}/schedule.json"])["prompt"] == schedule.prompt
    assert json.loads(files[f"task-experiences/{schedule.id}.json"])["schedule_id"] == schedule.id
    assert b"Discuss the current supplier quote" in files["comments.json"]
    assert json.loads(files[f"channel-deliveries/{delivery.id}.json"])["run_id"] == run.id
    assert {"automation", "feedback", "access", "collaboration"} <= {
        entry.category for entry in preview.entries
    }


def test_missing_legacy_artifact_version_is_an_explicit_conflict(source):
    service, project = source
    artifact = service.ledger.save_upload(project.id, "legacy.txt", b"registered bytes")
    service.ledger.update_project(project.id, lambda value: setattr(
        value.artifacts[0], "content_storage", "workspace",
    ))
    (service.ledger.workspace_for_project(project.id) / artifact.path).write_bytes(b"new content")
    with pytest.raises(ProjectExportConflict, match="integrity"):
        service.preview(project.id, None)


@pytest.mark.parametrize("change", ["workspace", "new_run", "new_schedule", "notification"])
def test_download_requires_fresh_preview_after_any_project_source_change(source, change):
    service, project = source
    preview = service.preview(project.id, None)
    ledger = service.ledger
    if change == "workspace":
        (ledger.workspace_for_project(project.id) / "new.txt").write_text("new project data")
    elif change == "new_run":
        finished_run(ledger, project.id)
    elif change == "new_schedule":
        ledger.create_schedule(Schedule(project_id=project.id, prompt="Review", cron="0 9 * * *"))
    else:
        notification = Notification(project_id=project.id, title="New result")
        ledger._atomic_write(
            ledger.root / "notifications" / f"{notification.id}.json", notification,
        )
    with pytest.raises(ProjectExportConflict, match="fresh export"):
        service.download(project.id, None, preview.fingerprint)


def test_unrelated_project_changes_do_not_invalidate_reviewed_export(source):
    service, project = source
    other = service.ledger.create_project(ProjectCreate(name="Other", objective="Independent"))
    run = finished_run(service.ledger, other.id)
    preview = service.preview(project.id, None)
    service.ledger.update_run(run.id, prompt="Other project changed its conversation")
    finished_run(service.ledger, other.id)
    service.ledger.create_schedule(Schedule(project_id=other.id, prompt="Other", cron="0 9 * * *"))
    assert service.download(project.id, None, preview.fingerprint).startswith(b"PK")


def test_mid_capture_source_changes_are_detected(source, monkeypatch):
    service, project = source
    workspace = service.ledger.workspace_for_project(project.id)
    (workspace / "note.txt").write_text("before")
    verify = project_export_capture.ExportCapture.verify

    def mutate_then_verify(capture):
        (workspace / "note.txt").write_text("after")
        verify(capture)

    monkeypatch.setattr(project_export_capture.ExportCapture, "verify", mutate_then_verify)
    with pytest.raises(ProjectExportConflict):
        service.preview(project.id, None)


def test_active_run_and_workspace_lease_prevent_export(source):
    service, project = source
    run = service.ledger.create_run(project.id, "Still queued")
    with pytest.raises(ProjectExportConflict, match="runs to finish"):
        service.preview(project.id, None)
    service.ledger.update_run(run.id, status=RunStatus.CANCELLED)
    with workspace_lease(service.ledger, project.id), pytest.raises(ValueError, match="in use"):
        service.preview(project.id, None)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_workspace_links_and_special_files_fail_closed(source, tmp_path, kind):
    service, project = source
    external = tmp_path / "private.txt"
    external.write_text("outside-source-canary")
    workspace = service.ledger.workspace_for_project(project.id)
    if kind == "symlink":
        (workspace / "unsafe.txt").symlink_to(external)
    elif kind == "hardlink":
        os.link(external, workspace / "unsafe.txt")
    else:
        os.mkfifo(workspace / "unsafe.txt")
    with pytest.raises((OSError, ValueError)):
        service.preview(project.id, None)


def test_corrupt_artifact_snapshot_or_receipt_cannot_be_exported_as_intact(source):
    service, project = source
    artifact = service.ledger.save_upload(project.id, "original.txt", b"original")
    snapshot = (
        service.ledger.root / "projects" / project.id / "artifact-versions" / artifact.checksum
    )
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"changed")
    with pytest.raises(ProjectExportConflict, match="integrity"):
        service.preview(project.id, None)
    snapshot.write_bytes(b"original")
    run = finished_run(service.ledger, project.id)
    receipt = service.ledger.issue_tool_receipt(
        project.id, run.id, tool="WebFetch", source_type=EvidenceSourceType.WEB,
        canonical_source="https://example.com/report", payload={"fact": "Original"},
        object_type="web_page_snapshot",
    )
    service.ledger._atomic_write(
        service.ledger.root / receipt.sanitized_payload_ref, {"fact": "Edit"},
    )
    with pytest.raises(ValueError, match="payload"):
        service.preview(project.id, None)
