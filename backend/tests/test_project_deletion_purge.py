"""Reviewed project erasure preserves other projects and content-free provenance."""

import json

import pytest
from test_project_templates import saved, services, use_request
from test_skill_proposals import approve, write_candidate

from ophira_backend.application_backup import create_backup, inspect_application, restore_backup
from ophira_backend.constants import RunStatus
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.project_deletion_capture import (
    ProjectDeletionConflict,
    capture_project_deletion,
)
from ophira_backend.project_deletion_proofs import deleted_project_ids, load_deletion_proofs
from ophira_backend.project_deletion_purge import purge_project_deletion
from ophira_backend.project_template_backup import inspect_project_templates
from ophira_backend.project_templates import TemplateUnavailable
from ophira_backend.skill_proposals import SkillProposalService
from ophira_backend.skills import SkillRegistry
from ophira_backend.storage import FileLedger


def persist(root, plan):
    folder = root / "project-deletions" / plan.project_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": plan.project_id,
                "status": "deleting",
                "plan": {"application": plan.model_dump(mode="json")},
            }
        )
    )


def project(ledger, name):
    return ledger.create_project(ProjectCreate(name=name, objective="synthetic deletion fixture"))


def test_purge_covers_project_run_snapshots_and_does_not_retain_content(tmp_path):
    ledger = FileLedger(tmp_path)
    first, second = project(ledger, "first"), project(ledger, "second")
    run = ledger.create_run(first.id, "private prompt canary")
    other = ledger.create_run(second.id, "retain other project")
    ledger.save_upload(first.id, "private.txt", b"private file canary")
    ledger.save_upload(second.id, "keep.txt", b"keep other bytes")
    plan = capture_project_deletion(tmp_path, first.id)
    serialized = plan.model_dump_json()
    assert "private prompt canary" not in serialized
    assert "private file canary" not in serialized
    assert plan.run_ids == [run.id]
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    assert not (tmp_path / "projects" / first.id).exists()
    assert not (tmp_path / "runs" / run.id).exists()
    assert ledger.get_run(other.id).prompt == "retain other project"
    assert ledger.get_project(second.id).id == second.id
    assert first.id not in (tmp_path / "_manifest.jsonl").read_text()
    assert second.id in (tmp_path / "_manifest.jsonl").read_text()
    assert load_deletion_proofs(tmp_path)[first.id].project_id == first.id


def test_unreviewed_change_is_rejected_before_any_erasure(tmp_path):
    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    upload = ledger.save_upload(item.id, "private.txt", b"before")
    plan = capture_project_deletion(tmp_path, item.id)
    path = ledger.workspace_for_project(item.id) / upload.path
    persist(tmp_path, plan)
    path.write_bytes(b"after")
    with pytest.raises(ProjectDeletionConflict, match="changed"):
        purge_project_deletion(tmp_path, plan)
    assert (tmp_path / "projects" / item.id / "project.json").exists()
    assert path.read_bytes() == b"after"


def test_new_file_and_hardlink_are_not_silently_erased(tmp_path):
    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    plan = capture_project_deletion(tmp_path, item.id)
    path = ledger.workspace_for_project(item.id) / "late.txt"
    persist(tmp_path, plan)
    path.write_bytes(b"late")
    with pytest.raises(ProjectDeletionConflict, match="New project files"):
        purge_project_deletion(tmp_path, plan)
    path.unlink()
    external = tmp_path / "outside.txt"
    external.write_bytes(b"external")
    path.hardlink_to(external)
    with pytest.raises(ProjectDeletionConflict, match="hard-linked"):
        capture_project_deletion(tmp_path, item.id)
    assert external.read_bytes() == b"external"


def test_owned_leaf_symlink_is_unlinked_without_reading_or_erasing_target(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    item = project(ledger, "first")
    external = tmp_path / "external.txt"
    external.write_bytes(b"do not erase")
    link = ledger.workspace_for_project(item.id) / "linked.txt"
    link.symlink_to(external)
    plan = capture_project_deletion(ledger.root, item.id)
    assert any(row.kind == "symlink" for row in plan.entries)
    persist(ledger.root, plan)
    purge_project_deletion(ledger.root, plan)
    assert external.read_bytes() == b"do not erase"
    assert not link.is_symlink()


def test_source_template_deletion_preserves_target_and_valid_backup_origin(tmp_path):
    store, applications = services(tmp_path)
    first = project(store.ledger, "source")
    template, _ = saved(store, first)
    target = applications.apply(first.id, template["id"], use_request(template), None)["project"]
    plan = capture_project_deletion(tmp_path, first.id)
    assert target.id in plan.extra_project_ids
    assert target.name not in plan.proofs.model_dump_json()
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    inspect_project_templates(tmp_path, {target.id: store.ledger.get_project(target.id)})
    assert store.ledger.get_project(target.id).template_origin.source_project_id == first.id
    # Deleting the independent target later reuses the same source proof.
    target_plan = capture_project_deletion(tmp_path, target.id)
    persist(tmp_path, target_plan)
    purge_project_deletion(tmp_path, target_plan)
    inspect_project_templates(tmp_path, {})


def test_target_template_deletion_erases_request_and_cannot_recreate_fixed_id(tmp_path):
    store, applications = services(tmp_path)
    first = project(store.ledger, "source")
    template, _ = saved(store, first)
    payload = use_request(template)
    target = applications.apply(first.id, template["id"], payload, None)["project"]
    plan = capture_project_deletion(tmp_path, target.id)
    assert first.id in plan.extra_project_ids
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    inspect_project_templates(tmp_path, {first.id: store.ledger.get_project(first.id)})
    assert applications.list(first.id, template["id"], None)["items"] == []
    with pytest.raises(TemplateUnavailable, match="permanently deleted"):
        applications.apply(first.id, template["id"], payload, None)
    assert not (tmp_path / "projects" / target.id).exists()


def test_published_skill_retains_registry_artifact_and_backup_proof(tmp_path):
    root, plugin = tmp_path / "data", tmp_path / "plugin"
    ledger = FileLedger(root)
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, root / "skills")
    registry.snapshot()
    proposals = SkillProposalService(ledger, registry)
    first = project(ledger, "source")
    run = ledger.create_run(first.id, "Synthetic Skill source")
    run = ledger.update_run(run.id, started_at=utc_now(), sdk_session_id="session_fixture")
    artifact, _ = write_candidate(ledger, first, run)
    proposal = approve(proposals, registry, proposals.submit_artifact(run.id, artifact.id))
    ledger.update_run(run.id, status=RunStatus.COMPLETED, finished_at=utc_now())
    plan = capture_project_deletion(root, first.id)
    assert plan.proofs.publications == [proposal.publication_receipt]
    persist(root, plan)
    purge_project_deletion(root, plan)
    state_path = root / "project-deletions" / first.id / "state.json"
    state = json.loads(state_path.read_text())
    state["status"] = "deleted"
    state_path.write_text(json.dumps(state))
    summary = inspect_application(root, plugin)
    assert summary["projects"] == [] and summary["proposals"] == {}
    assert registry.publication_receipt(proposal.id) == proposal.publication_receipt
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(root, plugin, backup)
    restore_backup(backup, restored, acknowledge_unreconciled_history=True)
    copied = inspect_application(restored / "data", restored / "application-plugin")
    assert copied["projects"] == []
    assert proposal.id in copied["canonical_state"]["publication_receipts"]


def test_empty_deletion_directory_is_not_a_committed_intent(tmp_path):
    folder = tmp_path / "project-deletions" / "project_fixture"
    folder.mkdir(parents=True)
    (folder / ".state.json.pending.tmp").write_text("partial")
    assert deleted_project_ids(tmp_path) == set()
    assert load_deletion_proofs(tmp_path) == {}


def test_tampered_proof_is_rejected(tmp_path):
    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    plan = capture_project_deletion(tmp_path, item.id)
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    path = tmp_path / "project-deletions" / item.id / "proofs.json"
    value = json.loads(path.read_text())
    value["project_id"] = "project_foreign"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="scope mismatch"):
        load_deletion_proofs(tmp_path)


def test_shared_agent_diary_removes_only_target_run_sections(tmp_path):
    ledger = FileLedger(tmp_path)
    first, second = project(ledger, "first"), project(ledger, "second")
    run = ledger.create_run(first.id, "private diary", agent_id="agent_fixture")
    other = ledger.create_run(second.id, "keep diary", agent_id="agent_fixture")
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    ledger.update_run(other.id, status=RunStatus.COMPLETED)
    directory = tmp_path / "agents/agent_fixture/diary"
    directory.mkdir(parents=True)
    path = directory / "2026-09-19.md"
    target = f"## 01:00:00 · {run.id}\n- 目标：private diary\n- 状态：completed\n\n"
    retained = f"## 02:00:00 · {other.id}\n- 目标：keep diary\n- 状态：completed\n\n"
    path.write_text("# User note\n\n" + target + retained)
    plan = capture_project_deletion(tmp_path, first.id)
    persist(tmp_path, plan)
    # Another project may append under the shared lock after preview.
    path.write_text(path.read_text() + "unrelated user note\n")
    purge_project_deletion(tmp_path, plan)
    assert path.read_text() == "# User note\n\n" + retained + "unrelated user note\n"


@pytest.mark.parametrize("shared", [False, True])
def test_companion_pairing_and_push_scrub_only_deleted_project(tmp_path, shared):
    import hashlib

    from ophira_backend.companion import CompanionStore, PairingCreate, PairingStatus
    from ophira_backend.models import Notification
    from ophira_backend.push_store import PushRecord, PushStore, PushWake

    ledger = FileLedger(tmp_path)
    first, second = project(ledger, "first"), project(ledger, "second")
    companions = CompanionStore(tmp_path / "companion-devices")
    ids = [first.id, second.id] if shared else [first.id]
    view, _secret = companions.create(PairingCreate(project_ids=ids), None)
    notice = Notification(project_id=first.id, title="private notification")
    ledger._atomic_write(tmp_path / "notifications" / f"{notice.id}.json", notice)
    anchor = hashlib.sha256(f"notification:{notice.id}".encode()).hexdigest()
    other = "a" * 64
    pushes = PushStore(tmp_path / "companion-push")
    push = PushRecord(
        id=view.id,
        public_key="public-fixture",
        known={anchor: utc_now(), other: utc_now()},
        wake=PushWake(anchors=[anchor, other]),
    )
    pushes._write(push)
    plan = capture_project_deletion(tmp_path, first.id)
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    pairing = companions._get(view.id)
    result = pushes.get(view.id)
    assert first.id not in pairing.project_ids
    assert anchor not in result.known
    assert result.generation != push.generation
    if shared:
        assert pairing.project_ids == [second.id]
        assert pairing.status == PairingStatus.OFFERED
        assert result.wake.anchors == [other]
        assert other in result.known
    else:
        assert pairing.project_ids == [] and pairing.status == PairingStatus.REVOKED
        assert pairing.link_secret_hash is None and pairing.cookie_secret_hash is None
        assert result.subscription is None and result.wake is None and result.known == {}


def test_deletion_rejects_new_project_record_even_outside_owned_trees(tmp_path):
    from ophira_backend.models import Notification

    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    plan = capture_project_deletion(tmp_path, item.id)
    persist(tmp_path, plan)
    notification = Notification(project_id=item.id, title="late private content")
    ledger._atomic_write(tmp_path / "notifications" / f"{notification.id}.json", notification)
    with pytest.raises(ProjectDeletionConflict, match="New project records"):
        purge_project_deletion(tmp_path, plan)
    assert (tmp_path / "projects" / item.id / "project.json").exists()


def test_diary_exact_length_handles_multiline_notes_and_keeps_adjacent_user_edits(tmp_path):
    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    run = ledger.create_run(item.id, "private diary", agent_id="agent_fixture")
    note = "private error\n\n## 03:00:00 · run_forged\nprivate continuation"
    ledger.update_run(run.id, status=RunStatus.FAILED, error=note)
    directory = tmp_path / "agents/agent_fixture/diary"
    directory.mkdir(parents=True)
    path = directory / "2026-09-19.md"
    target = f"## 01:00:00 · {run.id}\n- 目标：private diary\n- 状态：failed\n- 备注：{note}\n\n"
    path.write_text("user preface\n" + target + "user footer\n")
    plan = capture_project_deletion(tmp_path, item.id)
    assert "private error" not in plan.model_dump_json()
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    assert path.read_text() == "user preface\nuser footer\n"


def test_partial_file_erasure_can_resume_after_interruption(tmp_path, monkeypatch):
    import ophira_backend.project_deletion_purge as purge_module

    ledger = FileLedger(tmp_path)
    item = project(ledger, "first")
    ledger.save_upload(item.id, "one.txt", b"one")
    ledger.save_upload(item.id, "two.txt", b"two")
    plan = capture_project_deletion(tmp_path, item.id)
    persist(tmp_path, plan)
    original = purge_module.os.unlink
    removed = 0

    def interrupt(name, *args, **kwargs):
        nonlocal removed
        if name == "project.json":
            raise OSError("synthetic interrupted erase")
        result = original(name, *args, **kwargs)
        removed += 1
        return result

    with monkeypatch.context() as patch:
        patch.setattr(purge_module.os, "unlink", interrupt)
        with pytest.raises(ProjectDeletionConflict, match="retry cleanup"):
            purge_project_deletion(tmp_path, plan)
    assert removed > 0
    purge_project_deletion(tmp_path, plan)
    assert not (tmp_path / "projects" / item.id).exists()


def test_deleted_template_origin_cannot_be_changed_to_a_different_shared_copy(tmp_path):
    from ophira_backend.backup_files import BackupError

    store, applications = services(tmp_path)
    first = project(store.ledger, "source")
    template, _ = saved(store, first)
    target = applications.apply(first.id, template["id"], use_request(template), None)["project"]
    plan = capture_project_deletion(tmp_path, first.id)
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    changed = target.model_copy(
        update={"template_origin": target.template_origin.model_copy(update={"title": "Tampered"})}
    )
    with pytest.raises(BackupError, match="deleted_origin"):
        inspect_project_templates(tmp_path, {target.id: changed})


def test_projectless_notification_is_erased_by_run_link_and_foreign_link_rejected(tmp_path):
    from ophira_backend.models import Notification

    ledger = FileLedger(tmp_path)
    first, second = project(ledger, "first"), project(ledger, "second")
    run = ledger.create_run(first.id, "notification source")
    notice = Notification(run_id=run.id, title="private linked notification")
    path = tmp_path / "notifications" / f"{notice.id}.json"
    ledger._atomic_write(path, notice)
    plan = capture_project_deletion(tmp_path, first.id)
    assert path.relative_to(tmp_path).as_posix() in {row.path for row in plan.entries}
    ledger._atomic_write(path, notice.model_copy(update={"project_id": second.id}))
    with pytest.raises(ProjectDeletionConflict, match="conflicting project references"):
        capture_project_deletion(tmp_path, first.id)
    ledger._atomic_write(path, notice)
    persist(tmp_path, plan)
    purge_project_deletion(tmp_path, plan)
    assert not path.exists()


def test_stale_push_copy_erased_and_inflight_delivery_blocks_shared_rewrite(tmp_path):
    from ophira_backend.companion import CompanionStore, PairingCreate
    from ophira_backend.push_store import PushRecord, PushStore

    ledger = FileLedger(tmp_path)
    first, second = project(ledger, "first"), project(ledger, "second")
    companions = CompanionStore(tmp_path / "companion-devices")
    view, _secret = companions.create(PairingCreate(project_ids=[first.id, second.id]), None)
    pushes = PushStore(tmp_path / "companion-push")
    push = PushRecord(id=view.id, public_key="public-fixture")
    pushes._write(push)
    staging = pushes.root / ".push-interrupted"
    staging.write_text(push.model_dump_json())
    plan = capture_project_deletion(tmp_path, first.id)
    assert plan.companion_ids == [view.id]
    assert any(row.path == "companion-push/.push-interrupted" for row in plan.entries)
    persist(tmp_path, plan)
    with pushes.delivery_lease(view.id) as acquired:
        assert acquired
        with pytest.raises(ProjectDeletionConflict, match="busy"):
            purge_project_deletion(tmp_path, plan)
        assert companions._get(view.id).project_ids == [first.id, second.id]
    purge_project_deletion(tmp_path, plan)
    assert not staging.exists()
    assert companions._get(view.id).project_ids == [second.id]


def test_broken_manifest_symlink_is_not_treated_as_absent(tmp_path):
    ledger = FileLedger(tmp_path)
    first = project(ledger, "first")
    path = tmp_path / "_manifest.jsonl"
    path.unlink(missing_ok=True)
    path.symlink_to(tmp_path / "unavailable-external-file")
    with pytest.raises(ProjectDeletionConflict):
        capture_project_deletion(tmp_path, first.id)


def test_shared_raw_reads_count_toward_global_scan_budget(tmp_path):
    from ophira_backend.backup_files import BackupLimits
    from ophira_backend.project_deletion_capture import DeletionCapture

    path = tmp_path / "diary.md"
    path.write_bytes(b"12345678")
    capture = DeletionCapture(tmp_path, BackupLimits(total_bytes=3))
    assert capture.raw("diary.md", 32) == b"12345678"
    with pytest.raises(ProjectDeletionConflict, match="scan exceeds"):
        capture.raw("diary.md", 32)


def test_deletion_proof_scan_has_cumulative_byte_bound(tmp_path, monkeypatch):
    import ophira_backend.project_deletion_proofs as proofs_module

    ledger = FileLedger(tmp_path)
    first = project(ledger, "first")
    plan = capture_project_deletion(tmp_path, first.id)
    persist(tmp_path, plan)
    monkeypatch.setattr(proofs_module, "PROOF_SCAN_BYTES", 1)
    with pytest.raises(ValueError, match="scan exceeds"):
        load_deletion_proofs(tmp_path)
    with pytest.raises(ValueError, match="scan exceeds"):
        deleted_project_ids(tmp_path)
