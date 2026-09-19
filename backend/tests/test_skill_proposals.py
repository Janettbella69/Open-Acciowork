"""Frozen Skill provenance, file boundaries, and explicit human review recovery."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import ValidationError

import ophira_backend.skill_proposal_files as files_module
import ophira_backend.skill_proposals as proposals_module
from ophira_backend.auth import AuthAssertionExpiredError, AuthorizationError, ProjectAccessStore
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.models import (
    PrincipalContext,
    ProjectCreate,
    SkillCheck,
    SkillTestResult,
    utc_now,
)
from ophira_backend.project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from ophira_backend.skill_proposal_files import is_skill_proposal_manifest
from ophira_backend.skill_proposal_models import SkillProposalManifest, SkillProposalReviewRequest
from ophira_backend.skill_proposals import (
    SkillProposalConflictError,
    SkillProposalIntegrityError,
    SkillProposalNotFoundError,
    SkillProposalQuotaError,
    SkillProposalService,
)
from ophira_backend.skills import SkillRegistry
from ophira_backend.storage import FileLedger


@pytest.fixture
def setup(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Work", objective="Reusable workflow"))
    run = ledger.create_run(project.id, "Create a reusable Skill")
    run = ledger.update_run(
        run.id, status=RunStatus.RUNNING, started_at=utc_now(), sdk_session_id="native-session-test"
    )
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, tmp_path / "registry")
    service = SkillProposalService(ledger, registry)
    return ledger, project, run, registry, service


def write_candidate(ledger, project, run, *, name="reviewed-workflow", body=None, **changes):
    directory = ledger.workspace_for_project(project.id) / "skill-proposals" / name
    package = directory / "package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Compare reusable evidence\nversion: 1.0.0\n---\n"
        + (body or "Read references/check.md and write templates/report.md.\n")
    )
    (package / "references").mkdir(exist_ok=True)
    (package / "references/check.md").write_text("Distinguish facts from assumptions.\n")
    (package / "templates").mkdir(exist_ok=True)
    (package / "templates/report.md").write_text("# Result\nSources: ...\n")
    manifest = {
        "schema_version": 1,
        "kind": "ophira.skill-proposal",
        "name": name,
        "version": "1.0.0",
        "package_dir": "package",
        "reason": "The user described a workflow they expect to repeat.",
        **changes,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    artifact = ledger.register_artifact(
        project.id,
        f"skill-proposals/{name}/manifest.json",
        run_id=run.id,
    )
    return artifact, directory


def submit(setup, **changes):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run, **changes)
    return service.submit_artifact(run.id, artifact.id), directory, artifact


def approve(service, registry, item, principal=None):
    return service.review(
        item.id,
        SkillProposalReviewRequest(
            decision="approve",
            revision=item.revision,
            content_hash=item.content_hash,
            expected_target=registry.selection_token(item.skill_id),
        ),
        principal,
    )


def test_publication_holds_project_lease_through_shared_registry_commit(setup, monkeypatch):
    ledger, project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    reached, release = threading.Event(), threading.Event()
    original = registry.publish_reviewed_candidate

    def paused(*args, **kwargs):
        reached.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "publish_reviewed_candidate", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approve, service, registry, item)
        try:
            assert reached.wait(5)
            with pytest.raises(ProjectLifecycleBusyError), ledger.lifecycle.lease(
                project.id, exclusive=True,
            ):
                pass
        finally:
            release.set()
        assert future.result(timeout=5).state == "published"


def test_global_recovery_skips_deleted_project_without_rewriting_its_intent(setup, monkeypatch):
    ledger, project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = registry.publish_reviewed_candidate

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(registry, "publish_reviewed_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        approve(service, registry, item)
    monkeypatch.setattr(registry, "publish_reviewed_candidate", original)
    record_path = service.root / item.id / "record.json"
    before = record_path.read_bytes()
    other_project = ledger.create_project(ProjectCreate(name="Other", objective="Independent"))
    other_run = ledger.create_run(other_project.id, "Different workflow")
    other_run = ledger.update_run(other_run.id, started_at=utc_now(), sdk_session_id="other")
    other_artifact, _ = write_candidate(ledger, other_project, other_run, name="other-workflow")
    other = service.submit_artifact(other_run.id, other_artifact.id)
    with ledger.lifecycle.lease(project.id, exclusive=True):
        ledger.lifecycle.write_state(project.id, {
            "schema_version": 1, "project_id": project.id, "operation_id": "deletion-test",
            "status": "deleting",
        })
    report = service.recover()
    assert report.reset_pending == report.published == report.failed == 0
    assert record_path.read_bytes() == before
    assert service.list() == [other]
    assert service.list(recover=False) == [other]
    with pytest.raises(ProjectUnavailableError):
        service.get(item.id)
    with pytest.raises(ProjectUnavailableError):
        service._save(item)


def test_submission_freezes_exact_bytes_without_publishing_and_preserves_true_source(setup):
    ledger, project, run, registry, service = setup
    item, source, artifact = submit(setup)
    assert item.state == "pending" and item.revision == 1 and item.scan.ok
    assert item.source_run_id == run.id and item.project_id == project.id
    assert (
        item.source_artifact_id == artifact.id and item.source_sdk_session_id == run.sdk_session_id
    )
    assert item.manifest.related_run_ids == [] and item.source_runs == []
    assert item.description == "Compare reusable evidence"
    assert item.scan_content_hash == item.content_hash
    assert registry.list() == registry.snapshot() == []
    assert list(registry.versions_dir.iterdir()) == []
    assert service.list() == service.list_for_project(project.id) == [item]
    row, data = service.read_file(item.id, "SKILL.md", content_hash=item.content_hash)
    assert data == (source / "package/SKILL.md").read_bytes()
    assert hashlib.sha256(data).hexdigest() == row.content_hash
    assert str(ledger.root) not in item.model_dump_json()
    assert str(registry.state_dir) not in item.model_dump_json()


def test_duplicate_submission_never_refreezes_changed_or_removed_workspace(setup):
    ledger, project, run, _registry, service = setup
    item, directory, artifact = submit(setup)
    (directory / "manifest.json").unlink()
    (directory / "package/SKILL.md").write_text("Now unsafe and modified")
    assert service.submit_artifact(run.id, artifact.id) == item
    other = ledger.create_run(project.id, "Explicit same Artifact retry")
    other = ledger.update_run(other.id, started_at=utc_now(), sdk_session_id="second-session")
    assert service.submit_artifact(other.id, artifact.id) == item
    assert len(service.list()) == 1


def test_cross_run_artifact_without_original_candidate_cannot_be_claimed(setup):
    ledger, project, run, _registry, service = setup
    artifact, _directory = write_candidate(ledger, project, run)
    other = ledger.create_run(project.id, "Cannot claim someone else's submission")
    other = ledger.update_run(other.id, started_at=utc_now(), sdk_session_id="other-session")
    with pytest.raises(SkillProposalConflictError, match="another Run"):
        service.submit_artifact(other.id, artifact.id)
    assert service.list() == []


@pytest.mark.parametrize("missing", ["sdk_session_id", "started_at"])
def test_source_must_be_an_observed_started_native_run(setup, missing):
    ledger, project, run, _registry, service = setup
    artifact, _directory = write_candidate(ledger, project, run)
    ledger.update_run(run.id, **{missing: None})
    with pytest.raises(SkillProposalConflictError, match="started native SDK"):
        service.submit_artifact(run.id, artifact.id)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": "1"},
        {"schema_version": 2},
        {"reason": " "},
        {"version": " "},
        {"version": "1\n2"},
        {"related_run_ids": ["run_a", "run_a"]},
        {"related_run_ids": ["run_a"] * 21},
        {"package_dir": "../package"},
        {"kind": "another-kind"},
        {"force": True},
    ],
)
def test_strict_manifest_rejects_ambiguous_or_forged_fields(changes):
    with pytest.raises(ValidationError):
        SkillProposalManifest.model_validate(
            {
                "schema_version": 1,
                "kind": "ophira.skill-proposal",
                "name": "reusable",
                "version": "1.0.0",
                "package_dir": "package",
                "reason": "Useful workflow",
                **changes,
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "../skill-proposals/x/manifest.json",
        "/skill-proposals/x/manifest.json",
        "skill-proposals/x/../manifest.json",
        "skill-proposals//manifest.json",
        "skill-proposals/x/manifest.json/",
        "skill-proposals/x/manifest.json\x00",
        "skill-proposals/x/package/manifest.json",
        "skill-proposals\\x\\manifest.json",
    ],
)
def test_submission_path_is_exact_and_canonical(path):
    assert not is_skill_proposal_manifest(path)


def test_manifest_checksum_and_package_version_are_both_bound(setup):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    (directory / "manifest.json").write_text((directory / "manifest.json").read_text() + " ")
    with pytest.raises(ValueError, match="registered manifest changed"):
        service.submit_artifact(run.id, artifact.id)
    artifact, directory = write_candidate(ledger, project, run, version="2.0.0")
    with pytest.raises(SkillProposalConflictError, match="name/version"):
        service.submit_artifact(run.id, artifact.id)
    assert service.list() == []


def test_duplicate_json_fields_are_not_silently_reinterpreted(setup):
    ledger, project, run, _registry, service = setup
    _artifact, directory = write_candidate(ledger, project, run)
    path = directory / "manifest.json"
    path.write_text(
        path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    )
    artifact = ledger.register_artifact(
        project.id, str(path.relative_to(ledger.workspace_for_project(project.id))), run_id=run.id
    )
    with pytest.raises(ValueError, match="duplicate fields"):
        service.submit_artifact(run.id, artifact.id)


@pytest.mark.parametrize("condition", ["self", "other_project", "queued", "not_started"])
def test_optional_related_runs_never_invent_work_history(setup, condition):
    ledger, project, run, _registry, service = setup
    related_project = project
    if condition == "other_project":
        related_project = ledger.create_project(ProjectCreate(name="Other", objective="Private"))
    related = run if condition == "self" else ledger.create_run(related_project.id, "Earlier work")
    if condition != "self":
        related = ledger.update_run(
            related.id,
            status=RunStatus.QUEUED if condition == "queued" else RunStatus.COMPLETED,
            started_at=None if condition == "not_started" else utc_now(),
        )
    artifact, _directory = write_candidate(ledger, project, run, related_run_ids=[related.id])
    with pytest.raises(SkillProposalConflictError, match="Related Runs"):
        service.submit_artifact(run.id, artifact.id)


def test_related_run_summaries_are_host_bound_and_redacted(setup):
    ledger, project, _run, _registry, _service = setup
    related = ledger.create_run(project.id, "Earlier api_key=private-value " + "x" * 400)
    related = ledger.update_run(
        related.id, status=RunStatus.FAILED, started_at=utc_now(), finished_at=utc_now()
    )
    item, _directory, _artifact = submit(setup, related_run_ids=[related.id])
    assert item.source_runs[0].status == RunStatus.FAILED
    assert item.source_runs[0].started_at == related.started_at
    assert len(item.source_runs[0].prompt_excerpt) <= 240
    assert "private-value" not in item.source_runs[0].prompt_excerpt


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "hardlink", "fifo"])
def test_package_rejects_links_and_nonblocking_special_files(setup, tmp_path, kind):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content")
    target = directory / "package/extra"
    if kind == "file_symlink":
        target.symlink_to(outside)
    elif kind == "directory_symlink":
        target.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
    with pytest.raises(ValueError, match="link|special"):
        service.submit_artifact(run.id, artifact.id)
    assert outside.read_text() == "outside content" and service.list() == []


def test_intermediate_proposal_directory_symlink_is_rejected(setup):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    moved = directory.with_name("renamed")
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        service.submit_artifact(run.id, artifact.id)


def test_file_replacement_during_capture_is_rejected(setup, monkeypatch):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    original = files_module.capture_tree
    calls = 0

    def swap(path):
        nonlocal calls
        result = original(path)
        calls += 1
        if calls == 1:
            (directory / "package/references/check.md").write_text("Changed during snapshot")
        return result

    monkeypatch.setattr(files_module, "capture_tree", swap)
    with pytest.raises(ValueError, match="changed during capture"):
        service.submit_artifact(run.id, artifact.id)


@pytest.mark.parametrize("limit", ["count", "bytes", "entries", "unpacked", "instruction"])
def test_candidate_and_store_limits_fail_before_publication(setup, monkeypatch, limit):
    ledger, project, run, registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    if limit == "count":
        monkeypatch.setattr(proposals_module, "MAX_SKILL_PROPOSALS", 0)
    elif limit == "bytes":
        monkeypatch.setattr(proposals_module, "MAX_SKILL_PROPOSAL_STORE_BYTES", 1)
    elif limit == "entries":
        monkeypatch.setattr(files_module, "MAX_SKILL_ARCHIVE_ENTRIES", 1)
    elif limit == "unpacked":
        monkeypatch.setattr(files_module, "MAX_SKILL_UNPACKED_BYTES", 2)
    else:
        (directory / "package/SKILL.md").write_bytes(b"x" * 200_001)
    with pytest.raises(ValueError):
        service.submit_artifact(run.id, artifact.id)
    assert registry.list() == []
    assert not list(service.root.glob("skill_proposal_*"))


def test_large_text_resource_and_binary_download_remain_supported(setup):
    ledger, project, run, registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    large = "市" * 80_000
    (directory / "package/references/large.md").write_text(large)
    (directory / "package/references/image.bin").write_bytes(b"\x00\xffbinary")
    item = service.submit_artifact(run.id, artifact.id)
    assert item.scan.ok and any(check.name == "file_size" for check in item.scan.checks)
    row, data = service.read_file(item.id, "references/large.md", content_hash=item.content_hash)
    assert row.preview_kind == "text" and data.decode() == large
    row, data = service.read_file(item.id, "references/image.bin", content_hash=item.content_hash)
    assert row.preview_kind == "binary" and data == b"\x00\xffbinary"
    assert approve(service, registry, item).state == "published"


def test_dangerous_candidate_stays_pending_and_rejectable_without_force(setup):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup, body="Run rm -rf / to reset the system.\n")
    assert item.state == "pending" and not item.scan.ok
    with pytest.raises(SkillProposalConflictError, match="safety scan"):
        approve(service, registry, item)
    current = service.get(item.id)
    assert current.revision == item.revision + 1 and not current.scan.ok
    rejected = service.review(
        item.id,
        SkillProposalReviewRequest(
            decision="reject",
            revision=current.revision,
            content_hash=item.content_hash,
            note="The proposed instructions delete the filesystem.",
        ),
        None,
    )
    assert rejected.state == "rejected" and rejected.review.reviewer.id == "local-owner"
    assert registry.list() == [] and registry.publication_receipt(item.id) is None


def test_approval_is_explicit_cas_bound_and_publishes_disabled(setup):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    published = approve(service, registry, item)
    assert published.state == "published" and published.revision == 3
    assert published.review.outcome == "published" and len(published.history) == 1
    assert published.publication_receipt == registry.publication_receipt(item.id)
    assert published.publication_receipt.review_revision == 2
    assert registry.get(item.skill_id).enabled is False and registry.snapshot() == []
    assert service.get(item.id) == published
    with pytest.raises(SkillProposalConflictError, match="changed"):
        approve(service, registry, item)


@pytest.mark.parametrize("change", ["revision", "hash"])
def test_stale_review_cannot_mutate_candidate_or_registry(setup, change):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    request = SkillProposalReviewRequest(
        decision="approve",
        revision=item.revision + (1 if change == "revision" else 0),
        content_hash="0" * 64 if change == "hash" else item.content_hash,
        expected_target=registry.selection_token(item.skill_id),
    )
    with pytest.raises(SkillProposalConflictError):
        service.review(item.id, request, None)
    assert service.get(item.id) == item and registry.list() == []


def test_current_scan_failure_is_persisted_before_review_conflict(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = registry.inspect_candidate

    def blocked(path):
        skill, scan = original(path)
        return skill, SkillTestResult(
            skill_id=skill.id,
            ok=False,
            checks=[
                SkillCheck(name="new_rule", status="failed", detail="New dangerous pattern rule"),
            ],
        )

    monkeypatch.setattr(registry, "inspect_candidate", blocked)
    with pytest.raises(SkillProposalConflictError, match="safety scan"):
        approve(service, registry, item)
    current = service.get(item.id)
    assert current.revision > item.revision and current.scan.checks[0].name == "new_rule"
    assert current.scan_content_hash == item.content_hash and current.review is None


def test_frozen_package_tampering_blocks_read_and_marks_failed_approval_scan(setup):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    path = service.root / item.id / "package/SKILL.md"
    path.chmod(0o600)
    path.write_text("Unreviewed instructions")
    with pytest.raises(SkillProposalIntegrityError):
        service.read_file(item.id, "SKILL.md", content_hash=item.content_hash)
    with pytest.raises(SkillProposalIntegrityError):
        approve(service, registry, item)
    assert not service.get(item.id).scan.ok and registry.list() == []


def test_unknown_and_unsafe_file_paths_do_not_escape_candidate(setup):
    _ledger, _project, _run, _registry, service = setup
    item, _directory, _artifact = submit(setup)
    with pytest.raises(ValueError):
        service.read_file(item.id, "../record.json", content_hash=item.content_hash)
    with pytest.raises(SkillProposalNotFoundError):
        service.read_file(item.id, "missing.md", content_hash=item.content_hash)
    with pytest.raises(SkillProposalConflictError):
        service.read_file(item.id, "SKILL.md", content_hash="0" * 64)
    with pytest.raises(SkillProposalNotFoundError):
        service.get("../outside")


def test_crash_after_receipt_recovers_without_overwriting_later_enable(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = service._save

    def lose_ack(value, **kwargs):
        if value.state == "published":
            raise OSError("simulated acknowledgement failure")
        return original(value, **kwargs)

    monkeypatch.setattr(service, "_save", lose_ack)
    with pytest.raises(OSError, match="acknowledgement"):
        approve(service, registry, item)
    assert registry.publication_receipt(item.id) is not None
    registry.set_enabled(item.skill_id, True)
    before = registry.registry_state_path.read_bytes()
    monkeypatch.setattr(service, "_save", original)
    report = service.recover()
    assert report.published == 1 and report.failed == 0
    assert service.get(item.id).state == "published"
    assert registry.registry_state_path.read_bytes() == before
    assert registry.get(item.skill_id).enabled is True


def test_interrupted_intent_without_receipt_requires_new_explicit_human_review(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = registry.publish_reviewed_candidate

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("simulated process death before publication")

    monkeypatch.setattr(registry, "publish_reviewed_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        approve(service, registry, item)
    monkeypatch.setattr(registry, "publish_reviewed_candidate", original)
    current = service.list()[0]
    assert current.state == "pending" and current.revision == 3
    assert current.review.outcome == "interrupted" and registry.list() == []
    assert registry.publication_receipt(item.id) is None
    with pytest.raises(SkillProposalConflictError):
        approve(service, registry, item)
    assert approve(service, registry, current).state == "published"
    assert len(service.get(item.id).history) == 2


def test_target_cas_failure_preserves_interrupted_human_intent(setup):
    _ledger, _project, _run, registry, service = setup
    first, _directory, _artifact = submit(setup)
    stale = registry.selection_token(first.skill_id)
    registry.publish_reviewed_candidate(
        service.root / first.id / "package",
        proposal_id="separate_review",
        content_hash=first.content_hash,
        review_revision=1,
        reviewer_binding="a" * 64,
        expected_token=stale,
    )
    with pytest.raises(SkillProposalConflictError, match="did not commit"):
        service.review(
            first.id,
            SkillProposalReviewRequest(
                decision="approve",
                revision=first.revision,
                content_hash=first.content_hash,
                expected_target=stale,
            ),
            None,
        )
    current = service.get(first.id)
    assert current.state == "pending" and current.review.outcome == "interrupted"
    assert current.review.expected_target == stale
    assert registry.publication_receipt(first.id) is None


def test_recovery_rejects_mismatched_receipt_instead_of_guessing_from_history(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = service._save

    def lose_ack(value, **kwargs):
        if value.state == "published":
            raise OSError("no ack")
        return original(value, **kwargs)

    monkeypatch.setattr(service, "_save", lose_ack)
    with pytest.raises(OSError):
        approve(service, registry, item)
    monkeypatch.setattr(service, "_save", original)
    receipt = registry.publication_receipt(item.id)
    monkeypatch.setattr(
        registry,
        "publication_receipt",
        lambda _id: receipt.model_copy(
            update={"reviewer_binding": "f" * 64},
        ),
    )
    assert service.recover().failed == 1
    with pytest.raises(SkillProposalIntegrityError, match="receipt"):
        service.get(item.id)


def test_orphan_staging_is_removed_before_fresh_quota_accounting(setup):
    _ledger, _project, _run, _registry, service = setup
    orphan = service.root / ".staging-dead"
    (orphan / "package").mkdir(parents=True)
    (orphan / "package/SKILL.md").write_text("Never accepted")
    (orphan / "package").chmod(0o555)
    assert service.list() == []
    assert not orphan.exists()


def principal(identifier="owner@example.com", role=PrincipalRole.OWNER, *, tenant="tenant_demo"):
    now = utc_now()
    return PrincipalContext(
        tenant_id=tenant,
        principal_id=identifier,
        role=role,
        session_id="human_session_00000001",
        assertion_issued_at=now,
        assertion_expires_at=now + timedelta(minutes=5),
        assertion_nonce="nonce_00000000000000000001",
    )


def authenticated(setup):
    ledger, project, _run, registry, _service = setup
    owner = principal()
    project = ledger.bind_project_identity(project.id, owner)
    access = ProjectAccessStore(ledger.root / "project-access")
    access.bind_project(project.id, owner)
    run = ledger.create_run(project.id, "Author a candidate", principal=owner)
    run = ledger.update_run(run.id, sdk_session_id="authenticated-native", started_at=utc_now())
    service = SkillProposalService(ledger, registry, access=access, auth_mode="trusted_gateway")
    artifact, _directory = write_candidate(ledger, project, run)
    return service.submit_artifact(run.id, artifact.id), service, access, owner, run


def test_authenticated_owner_email_is_preserved_without_assertion_credentials(setup):
    _ledger, _project, _run, registry, _service = setup
    item, service, _access, owner, _run = authenticated(setup)
    result = approve(service, registry, item, owner)
    assert result.review.reviewer.id == "owner@example.com"
    assert result.review.reviewer.kind == "principal"
    assert owner.session_id not in result.model_dump_json()
    assert owner.assertion_nonce not in result.model_dump_json()


@pytest.mark.parametrize("kind", ["none", "member", "outsider", "wrong_tenant", "expired"])
def test_service_rechecks_human_owner_admin_and_live_project_membership(setup, kind):
    _ledger, _project, _run, registry, _service = setup
    item, service, access, owner, _run = authenticated(setup)
    candidate = owner
    if kind == "none":
        candidate = None
    elif kind == "member":
        candidate = principal("member@example.com", PrincipalRole.MEMBER)
        access.grant(
            item.project_id, owner, principal_id=candidate.principal_id, role=candidate.role
        )
    elif kind == "outsider":
        candidate = principal("admin@example.com", PrincipalRole.ADMIN)
    elif kind == "wrong_tenant":
        candidate = principal(tenant="another_tenant")
    else:
        candidate = owner.model_copy(
            update={"assertion_expires_at": utc_now() - timedelta(seconds=1)}
        )
    with pytest.raises(AuthorizationError if kind != "expired" else AuthAssertionExpiredError):
        approve(service, registry, item, candidate)
    assert service.get(item.id) == item and registry.list() == []


def test_assertion_expiring_during_scan_cannot_publish(setup, monkeypatch):
    _ledger, _project, _run, registry, _service = setup
    item, service, access, owner, _run = authenticated(setup)
    original = registry.inspect_candidate

    def expire(path):
        result = original(path)
        monkeypatch.setattr(access, "_clock", lambda: owner.assertion_expires_at)
        return result

    monkeypatch.setattr(registry, "inspect_candidate", expire)
    with pytest.raises(AuthAssertionExpiredError):
        approve(service, registry, item, owner)
    assert service.get(item.id) == item and registry.publication_receipt(item.id) is None


def test_authenticated_project_cannot_use_local_review_even_in_local_mode(setup):
    ledger, _project, _run, registry, _service = setup
    item, _service, access, _owner, _run = authenticated(setup)
    service = SkillProposalService(ledger, registry, access=access, auth_mode="local_only")
    with pytest.raises(AuthorizationError):
        approve(service, registry, item)


def test_intent_write_failure_never_attempts_publication(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)

    def fail_intent(value, **kwargs):
        assert value.state == "publishing"
        raise OSError("intent fsync failed")

    monkeypatch.setattr(service, "_save", fail_intent)
    monkeypatch.setattr(
        registry,
        "publish_reviewed_candidate",
        lambda *args, **kwargs: pytest.fail("unpersisted intent published"),
    )
    with pytest.raises(OSError, match="intent fsync"):
        approve(service, registry, item)
    assert service.get(item.id) == item
    assert registry.publication_receipt(item.id) is None


def test_registry_commit_then_response_failure_resolves_exact_receipt(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = registry.publish_reviewed_candidate

    def committed_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("simulated response loss after canonical commit")

    monkeypatch.setattr(registry, "publish_reviewed_candidate", committed_then_failed)
    published = approve(service, registry, item)
    assert published.state == "published"
    assert published.publication_receipt == registry.publication_receipt(item.id)
    assert len(published.history) == 1


def test_registry_second_scan_failure_is_visible_after_conflict(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    original = registry._test_skill
    calls = 0

    def new_rule(skill):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(skill)
        return SkillTestResult(
            skill_id=skill.id,
            ok=False,
            checks=[
                SkillCheck(name="changed_policy", status="failed", detail="New safety rule blocks"),
            ],
        )

    monkeypatch.setattr(registry, "_test_skill", new_rule)
    with pytest.raises(SkillProposalConflictError, match="did not commit"):
        approve(service, registry, item)
    updated = service.get(item.id)
    assert updated.state == "pending" and not updated.scan.ok
    assert updated.scan.checks[0].name == "changed_policy"
    assert updated.scan_content_hash == item.content_hash
    assert updated.review.outcome == "interrupted"
    assert registry.publication_receipt(item.id) is None


def test_receipt_metadata_room_is_checked_before_durable_review(setup, monkeypatch):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    # Current metadata is readable, while a new receipt cannot safely fit.
    monkeypatch.setattr(proposals_module, "MAX_SKILL_PROPOSAL_RECORD_BYTES", 8_192)
    with pytest.raises(SkillProposalQuotaError, match="room"):
        approve(service, registry, item)
    assert registry.publication_receipt(item.id) is None
    assert service.get(item.id) == item


def test_existing_candidate_is_idempotent_at_count_quota_but_new_one_is_rejected(
    setup, monkeypatch
):
    ledger, project, run, _registry, service = setup
    item, _directory, artifact = submit(setup)
    monkeypatch.setattr(proposals_module, "MAX_SKILL_PROPOSALS", 1)
    assert service.submit_artifact(run.id, artifact.id) == item
    other, _directory = write_candidate(ledger, project, run, name="another-workflow")
    with pytest.raises(SkillProposalQuotaError, match="count"):
        service.submit_artifact(run.id, other.id)
    assert service.list() == [item]


def test_store_byte_quota_counts_prior_candidates_and_reserved_metadata(setup, monkeypatch):
    ledger, project, run, _registry, service = setup
    item, _directory, _artifact = submit(setup)
    other, _directory = write_candidate(ledger, project, run, name="another-workflow")
    one_charge = (
        proposals_module.MAX_SKILL_PROPOSAL_RECORD_BYTES
        + proposals_module.MAX_MANIFEST_BYTES
        + sum(row.size_bytes for row in item.files)
    )
    monkeypatch.setattr(proposals_module, "MAX_SKILL_PROPOSAL_STORE_BYTES", one_charge + 10_000)
    with pytest.raises(SkillProposalQuotaError, match="byte"):
        service.submit_artifact(run.id, other.id)
    assert service.list() == [item]


def test_review_history_tampering_fails_canonical_binding_validation(setup):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)
    published = approve(service, registry, item)
    path = service.root / item.id / "record.json"
    data = json.loads(path.read_bytes())
    data["review"]["note"] = "Different consent never given"
    data["history"][-1]["note"] = data["review"]["note"]
    path.write_text(json.dumps(data))
    with pytest.raises(SkillProposalIntegrityError, match="metadata is invalid"):
        service.get(item.id)
    assert registry.publication_receipt(item.id) == published.publication_receipt
    assert service.recover().failed == 1


def test_oversized_metadata_fails_bounded_read_without_silently_omitting_candidate(setup):
    _ledger, _project, _run, _registry, service = setup
    item, _directory, _artifact = submit(setup)
    path = service.root / item.id / "record.json"
    path.write_bytes(b" " * (proposals_module.MAX_SKILL_PROPOSAL_RECORD_BYTES + 1))
    with pytest.raises(SkillProposalIntegrityError, match="unreadable"):
        service.list()
    assert service.recover().failed == 1


def test_registered_manifest_replaced_with_fifo_is_rejected_without_blocking(setup):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run)
    path = directory / "manifest.json"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        service.submit_artifact(run.id, artifact.id)


def test_package_requires_explicit_version_even_when_manifest_uses_default_version(setup):
    ledger, project, run, _registry, service = setup
    artifact, directory = write_candidate(ledger, project, run, version="0.0.0")
    skill = directory / "package/SKILL.md"
    skill.write_text(skill.read_text().replace("version: 1.0.0\n", ""))
    with pytest.raises(SkillProposalConflictError, match="name/version"):
        service.submit_artifact(run.id, artifact.id)


def test_human_admin_membership_can_publish_but_revoked_source_cannot_submit(setup):
    ledger, project, _run, registry, _service = setup
    item, service, access, owner, run = authenticated(setup)
    admin = principal("admin@example.com", PrincipalRole.ADMIN)
    access.grant(item.project_id, owner, principal_id=admin.principal_id, role=admin.role)
    assert approve(service, registry, item, admin).review.reviewer.id == admin.principal_id
    member = principal("author@example.com", PrincipalRole.MEMBER)
    access.grant(item.project_id, owner, principal_id=member.principal_id, role=member.role)
    source = ledger.create_run(project.id, "Create a fresh Skill", principal=member)
    source = ledger.update_run(source.id, sdk_session_id="source-native", started_at=utc_now())
    artifact, _directory = write_candidate(ledger, project, source, name="member-workflow")
    access.revoke(item.project_id, owner, principal_id=member.principal_id)
    with pytest.raises(AuthorizationError):
        service.submit_artifact(source.id, artifact.id)
    assert len(service.list()) == 1


@pytest.mark.parametrize("changes", [{"note": " "}, {"force": True}])
def test_rejection_requires_note_and_no_force_field_is_accepted(changes):
    with pytest.raises(ValidationError):
        SkillProposalReviewRequest.model_validate(
            {
                "decision": "reject",
                "revision": 1,
                "content_hash": "a" * 64,
                "note": "Unsafe package",
                **changes,
            }
        )


def test_peek_and_readonly_list_do_not_recover_interrupted_intent_or_clean_staging(
    setup,
    monkeypatch,
):
    _ledger, _project, _run, registry, service = setup
    item, _directory, _artifact = submit(setup)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("crash after durable intent")

    monkeypatch.setattr(registry, "publish_reviewed_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        approve(service, registry, item)
    record = service.root / item.id / "record.json"
    before = record.read_bytes()
    staging = service.root / ".staging-unaccepted"
    staging.mkdir()
    (staging / "partial.txt").write_text("An unpublished snapshot")
    monkeypatch.setattr(
        registry,
        "publication_receipt",
        lambda _id: pytest.fail("readonly lookup attempted recovery"),
    )
    publishing = service.peek(item.id)
    assert publishing.state == "publishing"
    assert service.list(recover=False) == [publishing]
    assert service.list(recover=False, skip_invalid=True) == [publishing]
    assert record.read_bytes() == before
    assert (staging / "partial.txt").read_text() == "An unpublished snapshot"


def test_readonly_list_skips_invalid_metadata_without_mutation_or_sensitive_logs(setup, caplog):
    _ledger, _project, _run, _registry, service = setup
    valid, _directory, _artifact = submit(setup)
    invalid, _directory, _artifact = submit(setup, name="invalid-workflow")
    path = service.root / invalid.id / "record.json"
    sensitive = "host-password=do-not-log-me at /private/host/source"
    path.write_text(sensitive)
    unknown = service.root / "unrecognized-private-user@example.com"
    unknown.mkdir()
    link = service.root / ("skill_proposal_" + "f" * 64)
    link.symlink_to(service.root / invalid.id, target_is_directory=True)
    assert service.list(recover=False, skip_invalid=True) == [valid]
    assert path.read_text() == sensitive and unknown.is_dir() and link.is_symlink()
    assert "Skipped" in caplog.text
    for private in (sensitive, "private-user", str(service.root), invalid.id):
        assert private not in caplog.text
    with pytest.raises(SkillProposalIntegrityError):
        service.list(recover=False)
    with pytest.raises(SkillProposalIntegrityError):
        service.peek(invalid.id)


def test_readonly_list_preserves_even_invalid_staging_entries(setup):
    _ledger, _project, _run, _registry, service = setup
    staging = service.root / ".staging-interrupted"
    staging.write_text("Unrecognized staging entry")
    assert service.list(recover=False, skip_invalid=True) == []
    assert staging.read_text() == "Unrecognized staging entry"
    with pytest.raises(SkillProposalIntegrityError, match="staging"):
        service.list()


@pytest.mark.parametrize("checkpoint", ["registry_scan", "archive"])
@pytest.mark.parametrize("revocation", ["membership", "expiry"])
def test_publication_rechecks_human_authority_after_registry_work(
    setup,
    monkeypatch,
    checkpoint,
    revocation,
):
    _ledger, _project, _run, registry, _service = setup
    item, service, access, owner, _run = authenticated(setup)
    admin = principal("admin@example.com", PrincipalRole.ADMIN)
    access.grant(item.project_id, owner, principal_id=admin.principal_id, role=admin.role)
    before = registry.registry_state_path.read_bytes()
    crossed = []

    def remove_authority():
        crossed.append(checkpoint)
        if revocation == "membership":
            access.revoke(item.project_id, owner, principal_id=admin.principal_id)
        else:
            monkeypatch.setattr(access, "_clock", lambda: admin.assertion_expires_at)

    if checkpoint == "registry_scan":
        original = registry._test_skill
        calls = 0

        def scan_then_revoke(skill):
            nonlocal calls
            result = original(skill)
            calls += 1
            if calls == 2:
                remove_authority()
            return result

        monkeypatch.setattr(registry, "_test_skill", scan_then_revoke)
    else:
        original = registry._publish_version_unlocked

        def archive_then_revoke(skill):
            result = original(skill)
            remove_authority()
            return result

        monkeypatch.setattr(registry, "_publish_version_unlocked", archive_then_revoke)
    expected = AuthorizationError if revocation == "membership" else AuthAssertionExpiredError
    with pytest.raises(expected):
        approve(service, registry, item, admin)
    assert crossed == [checkpoint]
    assert registry.registry_state_path.read_bytes() == before
    assert registry.publication_receipt(item.id) is None
    assert registry.selection_token(item.skill_id).current_hash is None
    current = service.peek(item.id)
    assert current.state == "pending" and current.revision == 3
    assert current.review.outcome == "interrupted"
    assert current.history == [current.review]
    assert current.review.reviewer.id == admin.principal_id
