import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_project_session_cleanup import native_session, write

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.automation import NotificationStore
from ophira_backend.constants import RunStatus
from ophira_backend.conversation_deletion import ConversationDeletionService
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.project_deletion import ProjectDeletionUncertainError
from ophira_backend.project_deletion_capture import ProjectDeletionConflict
from ophira_backend.project_deletion_models import ProjectDeletionRequest
from ophira_backend.project_lifecycle import ProjectUnavailableError
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def setup(root):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(name="private project name", objective="fixture"))
    native, config, directory = native_session(root, project.id)
    first = ledger.create_run(project.id, "private conversation prompt")
    first = ledger.update_run(
        first.id,
        status=RunStatus.COMPLETED,
        sdk_session_id=native.sdk_session_id,
        started_at=utc_now(),
    )
    resumed = ledger.create_run(
        project.id,
        "private followup prompt",
        resume_session_id=first.sdk_session_id,
        resume_source_run_id=first.id,
    )
    resumed = ledger.update_run(resumed.id, status=RunStatus.FAILED)
    other_native, _, _ = native_session(root, project.id)
    other = ledger.create_run(project.id, "keep another conversation")
    other = ledger.update_run(
        other.id, status=RunStatus.COMPLETED, sdk_session_id=other_native.sdk_session_id
    )
    write(config / "file-history" / first.sdk_session_id / "checkpoint", b"private checkpoint")
    write(directory / first.sdk_session_id / "subagents" / "agent.jsonl")
    ledger.save_upload(project.id, "retained.txt", b"retained project upload")
    service = ConversationDeletionService(ledger, ProjectAccessStore(root / "auth" / "projects"))
    return ledger, service, project, first, resumed, other, directory


def request_for(service, project, run):
    preview = service.preview(project.id, run.id, None)
    assert preview.can_delete, preview
    return ProjectDeletionRequest(
        operation_id=str(uuid4()),
        fingerprint=preview.fingerprint,
        confirmation_name=project.name,
        reason="private deletion reason",
    )


def test_whole_chain_is_erased_other_conversation_and_project_survive(tmp_path):
    ledger, service, project, first, resumed, other, directory = setup(tmp_path)
    inbox = NotificationStore(tmp_path / "notifications")
    removed = inbox.add(title="private notification", project_id=project.id, run_id=first.id)
    kept = inbox.add(title="retained notification", project_id=project.id, run_id=other.id)
    diary = ledger.diary_dir(first) / "2026-09-19.md"
    first_entry = f"## 01:00:00 · {first.id}\n- 目标：{first.prompt}\n- 状态：completed\n\n"
    other_entry = f"## 02:00:00 · {other.id}\n- 目标：{other.prompt}\n- 状态：completed\n\n"
    diary.write_text(first_entry + other_entry + "Retain this manual project note.\n")
    request = request_for(service, project, resumed)
    result = service.start(project.id, resumed.id, request, None)
    assert result.status == "deleted", result
    assert set(result.run_ids) == {first.id, resumed.id}
    assert service.start(project.id, resumed.id, request, None) == result
    assert service.retry(project.id, request.operation_id, None) == result
    assert service.get(project.id, request.operation_id, None) == result
    assert ledger.get_project(project.id).name == project.name
    assert ledger.get_run(other.id).prompt == other.prompt
    assert {run.id for run in ledger.list_runs(project.id)} == {other.id}
    assert (directory / f"{other.sdk_session_id}.jsonl").exists()
    assert not (directory / f"{first.sdk_session_id}.jsonl").exists()
    assert not inbox._path(removed.id).exists()
    assert inbox.get(kept.id) == kept
    assert diary.read_text() == other_entry + "Retain this manual project note.\n"
    for run in (first, resumed):
        assert not (tmp_path / "runs" / run.id).exists()
        with pytest.raises(LedgerNotFoundError):
            ledger.get_run(run.id)
        with pytest.raises(LedgerNotFoundError):
            ledger.create_run(project.id, "revival", run_id=run.id)
    with pytest.raises(LedgerNotFoundError):
        ledger.create_run(project.id, "resume revival", resume_session_id=first.sdk_session_id)
    with pytest.raises(ProjectUnavailableError):
        inbox._write(removed)
    fresh = ledger.create_run(project.id, "fresh conversation")
    assert fresh.id != first.id
    raw = next((tmp_path / "conversation-deletions" / project.id).glob("*.json")).read_text()
    for private in (project.name, first.prompt, resumed.prompt, request.reason, removed.title):
        assert private not in raw
    assert {
        row["run_id"] for row in map(json.loads, ledger.manifest_path.read_text().splitlines())
    } == {
        other.id,
        fresh.id,
    }


@pytest.mark.parametrize("change", ["prompt", "chain", "confirmation", "nonterminal"])
def test_changed_or_active_conversation_cannot_commit(tmp_path, change):
    ledger, service, project, first, _resumed, _other, directory = setup(tmp_path)
    request = request_for(service, project, first)
    if change == "prompt":
        ledger.update_run(first.id, prompt="changed prompt")
    elif change == "chain":
        ledger.create_run(project.id, "new followup", resume_session_id=first.sdk_session_id)
    elif change == "confirmation":
        request.confirmation_name = "wrong"
    else:
        ledger.update_run(first.id, status=RunStatus.RUNNING)
    with pytest.raises(ProjectDeletionConflict):
        service.start(project.id, first.id, request, None)
    assert not service.states.list(project.id)
    assert (directory / f"{first.sdk_session_id}.jsonl").exists()


def test_active_project_lease_blocks_review_and_no_intent_is_written(tmp_path):
    ledger, service, project, first, *_ = setup(tmp_path)
    ready, finish = threading.Event(), threading.Event()

    def worker():
        with ledger.lifecycle.lease(project.id):
            ready.set()
            assert finish.wait(10)

    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(worker)
        try:
            assert ready.wait(5)
            assert not service.preview(project.id, first.id, None).can_delete
            assert service.states.list(project.id) == []
        finally:
            finish.set()
            future.result(5)


def test_interruption_blocks_all_project_work_until_recovery(tmp_path, monkeypatch):
    import ophira_backend.conversation_deletion as module

    ledger, service, project, first, _resumed, other, directory = setup(tmp_path)
    request = request_for(service, project, first)
    actual = module.purge_conversation

    def crash(*_args):
        raise OSError("private disk error canary")

    monkeypatch.setattr(module, "purge_conversation", crash)
    result = service.start(project.id, first.id, request, None)
    assert result.status == "blocked" and result.can_retry
    assert "private disk error" not in result.model_dump_json()
    assert not (directory / f"{first.sdk_session_id}.jsonl").exists()
    with pytest.raises(LedgerNotFoundError):
        ledger.create_run(project.id, "late run")
    with pytest.raises(LedgerNotFoundError):
        ledger.get_run(other.id)
    monkeypatch.setattr(module, "purge_conversation", actual)
    recovered = ConversationDeletionService(FileLedger(tmp_path), service.projects.access)
    recovered.recover()
    assert recovered.get(project.id, request.operation_id, None).status == "deleted"
    assert ledger.get_run(other.id) == other


def test_fsync_ambiguity_keeps_same_operation_and_never_claims_rejection(tmp_path, monkeypatch):
    ledger, service, project, first, *_ = setup(tmp_path)
    request = request_for(service, project, first)
    write_state = service.states.write

    def uncertain(state):
        write_state(state)
        raise OSError("after durable rename")

    monkeypatch.setattr(service.states, "write", uncertain)
    with pytest.raises(ProjectDeletionUncertainError):
        service.start(project.id, first.id, request, None)
    assert service.get(project.id, request.operation_id, None).status == "deleting"
    with pytest.raises(LedgerNotFoundError):
        ledger.get_project(project.id)
    monkeypatch.setattr(service.states, "write", write_state)
    assert service.retry(project.id, request.operation_id, None).status == "deleted"


def test_retained_workspace_checkpoint_uses_non_resumable_origin(tmp_path):
    from ophira_backend.workspace_checkpoints import WorkspaceCheckpoints

    ledger, service, project, first, *_ = setup(tmp_path)
    checkpoints = WorkspaceCheckpoints(ledger)
    ledger.update_run(first.id, started_at=None)
    checkpoint = checkpoints.capture(project.id, run_id=first.id)
    ledger.update_run(first.id, started_at=first.started_at)
    request = request_for(service, project, first)
    assert service.start(project.id, first.id, request, None).status == "deleted"
    assert checkpoints.load(project.id, checkpoint.id) == checkpoint
    assert checkpoints.list(project.id)[0].run_id == first.id
    with pytest.raises(LedgerNotFoundError):
        ledger.get_run(first.id)


def test_second_conversation_and_then_project_can_be_deleted(tmp_path):
    ledger, service, project, first, _resumed, other, _directory = setup(tmp_path)
    for source in (first, other):
        request = request_for(service, project, source)
        assert service.start(project.id, source.id, request, None).status == "deleted"
    assert ledger.list_runs(project.id) == []
    preview = service.projects.preview(project.id, None)
    assert preview.can_delete
    request = ProjectDeletionRequest(
        operation_id=str(uuid4()),
        fingerprint=preview.fingerprint,
        confirmation_name=project.name,
        reason="remove project",
    )
    assert service.projects.start(project.id, request, None).status == "deleted"


def test_cold_backup_preserves_skill_provenance_after_conversation_erasure(tmp_path):
    import io
    import zipfile

    from test_skill_proposals import approve, write_candidate

    from ophira_backend.application_backup import create_backup, restore_backup
    from ophira_backend.backup_files import remove_staging
    from ophira_backend.config import Settings
    from ophira_backend.constants import EvidenceSourceType
    from ophira_backend.project_exports import ProjectExports
    from ophira_backend.skill_proposals import SkillProposalService
    from ophira_backend.skills import SkillRegistry

    data = tmp_path / "data"
    ledger, deletion, project, first, resumed, *_ = setup(data)
    ledger.update_run(resumed.id, started_at=utc_now())
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, data / "skills")
    proposals = SkillProposalService(ledger, registry)
    artifact, _ = write_candidate(
        ledger,
        project,
        first,
        name="retained-conversation-skill",
        related_run_ids=[resumed.id],
    )
    proposal = proposals.submit_artifact(first.id, artifact.id)
    proposal = approve(proposals, registry, proposal)
    receipt = ledger.issue_tool_receipt(
        project.id,
        first.id,
        tool="WebFetch",
        source_type=EvidenceSourceType.WEB,
        canonical_source="https://example.com/fixture",
        payload={"quote": "Retained source"},
        object_type="web_page_snapshot",
    )
    request = request_for(deletion, project, first)
    assert deletion.start(project.id, first.id, request, None).status == "deleted"
    export = ProjectExports(
        ledger, deletion.projects.access, Settings(_env_file=None, data_dir=data)
    )
    review = export.preview(project.id, None)
    with zipfile.ZipFile(
        io.BytesIO(export.download(project.id, None, review.fingerprint))
    ) as archive:
        assert not any(name.startswith(f"runs/{first.id}/") for name in archive.namelist())
        assert any(receipt.id in name for name in archive.namelist())
        origins = archive.read("deleted-conversations/run-origins.json").decode()
        assert first.prompt not in origins and resumed.prompt not in origins
    archive = tmp_path / "backup"
    restored = tmp_path / "restored"
    try:
        assert create_backup(data, plugin, archive)["published"]
        assert restore_backup(
            archive, restored, acknowledge_unreconciled_history=True,
        )["published"]
        restored_ledger = FileLedger(restored / "data")
        restored_proposals = SkillProposalService(
            restored_ledger,
            SkillRegistry(restored / "application-plugin", restored / "data" / "skills"),
        )
        assert (
            restored_proposals.peek(proposal.id).publication_receipt == proposal.publication_receipt
        )
        with pytest.raises(LedgerNotFoundError):
            restored_ledger.get_run(first.id)
    finally:
        for directory in (data, plugin, archive, restored):
            if directory.exists():
                remove_staging(directory)
