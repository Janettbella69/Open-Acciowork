"""Cold snapshots bind project provenance, proposals and canonical publication."""

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_skill_proposals import approve, write_candidate

from ophira_backend import application_backup as backup
from ophira_backend.backup_files import BackupError, BackupLimits, remove_staging, tree_inventory
from ophira_backend.constants import RunStatus
from ophira_backend.maintenance import (
    MAINTENANCE_LOCK,
    DataDirectoryBusyError,
    data_directory_lease,
)
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.skill_proposal_models import SkillProposalReviewRequest
from ophira_backend.skill_proposals import SkillProposalService
from ophira_backend.skills import SkillRegistry
from ophira_backend.storage import FileLedger


class SimulatedCrash(BaseException):
    pass


@pytest.fixture
def source(tmp_path, monkeypatch):
    data, plugin = tmp_path / "data", tmp_path / "plugin"
    ledger = FileLedger(data)
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, data / "skills")
    registry.snapshot()
    service = SkillProposalService(ledger, registry)
    project = ledger.create_project(ProjectCreate(name="Cold backup fixture", objective="Recovery"))
    run = ledger.create_run(project.id, "Synthetic proposal source; no Provider invocation")
    ledger.update_run(run.id, started_at=utc_now(), sdk_session_id="synthetic-backup-session")
    items = {}
    for state in ("pending", "rejected", "published", "intent_only", "committed_intent"):
        artifact, _ = write_candidate(ledger, project, run, name=f"backup-{state}")
        item = service.submit_artifact(run.id, artifact.id)
        if state == "rejected":
            item = service.review(
                item.id,
                SkillProposalReviewRequest(
                    decision="reject",
                    revision=item.revision,
                    content_hash=item.content_hash,
                    note="Synthetic human rejection",
                    expected_target=None,
                ),
                None,
            )
        elif state == "published":
            item = approve(service, registry, item)
            registry.set_enabled(item.skill_id, True)
        elif state in {"intent_only", "committed_intent"}:

            def crash(*args, **kwargs):
                raise SimulatedCrash()

            with monkeypatch.context() as patch:
                if state == "intent_only":
                    patch.setattr(registry, "publish_reviewed_candidate", crash)
                else:
                    patch.setattr(service, "_finish", crash)
                with pytest.raises(SimulatedCrash):
                    approve(service, registry, item)
            item = service.peek(item.id)
            assert item.state == "publishing"
        items[state] = item
    pins = registry.snapshot()
    ledger.update_run(
        run.id, status=RunStatus.COMPLETED, finished_at=utc_now(), skill_snapshot=pins
    )
    (data / "secrets").mkdir()
    (data / "secrets/provider-secrets.json").write_text('{"fixture":"private-backup-canary"}')
    (data / "auth/projects").mkdir(parents=True)
    (data / "auth/projects/fixture-policy.json").write_text('{"fixture":"retain-auth-state"}')
    (data / "empty-store").mkdir()
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "tool.sh").write_text("#!/bin/sh\ntrue\n")
    (workspace / "tool.sh").chmod(0o755)
    yield data, plugin, ledger, registry, service, project, run, items
    # Immutable source/restored packages deliberately retain read-only directories.
    for child in tmp_path.iterdir():
        if child.is_dir() and not child.is_symlink():
            remove_staging(child)


def make_backup(source, tmp_path):
    data, plugin, *_ = source
    target = tmp_path / "backup"
    result = backup.create_backup(data, plugin, target)
    assert result["published"]
    return target


def rehash(archive):
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tree"] = tree_inventory(archive / "payload", backup.DEFAULT_LIMITS)
    manifest_path.write_text(json.dumps(manifest))
    return manifest


def test_complete_snapshot_preserves_source_and_recovers_only_proposal_intents(source, tmp_path):
    data, plugin, ledger, registry, service, project, run, items = source
    before = tree_inventory(data, backup.DEFAULT_LIMITS)
    archive = make_backup(source, tmp_path)
    assert (
        tree_inventory(data, backup.DEFAULT_LIMITS, omit_root=frozenset({MAINTENANCE_LOCK}))
        == before
    )
    assert service.peek(items["intent_only"].id).state == "publishing"
    canonical = registry.registry_state_path.read_bytes()
    remove_staging(data)
    remove_staging(plugin)
    target = tmp_path / "restored"
    result = backup.restore_backup(archive, target)
    assert result["published"] and result["canonical_preserved"]
    assert result["proposal_states"] == {"pending": 2, "published": 2, "rejected": 1}
    assert result["proposal_recovery"] == {
        "inspected": 5,
        "published": 1,
        "reset_pending": 1,
        "failed": 0,
    }
    restored_ledger = FileLedger(target / "data")
    restored_registry = SkillRegistry(target / "application-plugin", target / "data/skills")
    restored_service = SkillProposalService(restored_ledger, restored_registry)
    assert restored_registry.registry_state_path.read_bytes() == canonical
    assert restored_ledger.get_run(run.id).status == RunStatus.COMPLETED
    assert restored_registry.get(items["published"].skill_id).enabled is True
    assert restored_registry.get(items["committed_intent"].skill_id).enabled is False
    assert restored_service.peek(items["pending"].id) == items["pending"]
    assert restored_service.peek(items["rejected"].id) == items["rejected"]
    fresh = restored_service.peek(items["intent_only"].id)
    assert fresh.state == "pending" and fresh.revision == items["intent_only"].revision + 1
    assert fresh.history[-1].outcome == "interrupted"
    assert (
        target / "data/secrets/provider-secrets.json"
    ).read_text() == '{"fixture":"private-backup-canary"}'
    assert (target / "data/empty-store").is_dir()
    assert (
        restored_ledger.workspace_for_project(project.id) / "tool.sh"
    ).stat().st_mode & 0o777 == 0o700
    for path in (archive, target):
        assert path.stat().st_mode & 0o077 == 0
        assert all(child.stat().st_mode & 0o077 == 0 for child in path.rglob("*"))
    with restored_registry.runtime_snapshot_lease(
        restored_ledger.get_run(run.id).skill_snapshot
    ) as view:
        assert all(Path(row["path"]).is_relative_to(target) for row in view.plugin_paths)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("artifact_removed", "proposal_artifact_mismatch"),
        ("run_session_changed", "proposal_run_mismatch"),
        ("proposal_removed", "publication_proposal_missing"),
        ("receipt_removed", "proposal_publication_receipt_missing"),
        ("nonterminal_run", "nonterminal_run_present"),
        ("missing_project", "run_project_mismatch"),
        ("malformed_run", None),
        ("frozen_file_changed", None),
    ],
)
def test_transport_consistent_but_incoherent_state_never_publishes(
    source, tmp_path, failure, reason
):
    *_, project, run, items = source
    archive = make_backup(source, tmp_path)
    data = archive / "payload/data"
    project_path = data / "projects" / project.id / "project.json"
    run_path = data / "runs" / run.id / "run.json"
    state_path = data / "skills/registry-state.json"
    if failure == "artifact_removed":
        value = json.loads(project_path.read_text())
        value["artifacts"] = []
        project_path.write_text(json.dumps(value))
    elif failure in {"run_session_changed", "nonterminal_run"}:
        value = json.loads(run_path.read_text())
        value.update(
            {"sdk_session_id": "different-session"}
            if failure == "run_session_changed"
            else {"status": RunStatus.RUNNING.value}
        )
        run_path.write_text(json.dumps(value))
    elif failure == "proposal_removed":
        remove_staging(data / "skill-proposals" / items["published"].id)
    elif failure == "receipt_removed":
        value = json.loads(state_path.read_text())
        del value["publication_receipts"][items["published"].id]
        state_path.write_text(json.dumps(value))
    elif failure == "missing_project":
        remove_staging(project_path.parent)
    elif failure == "malformed_run":
        run_path.write_text('{"invalid":"private-value-must-not-be-logged"}')
    else:
        path = data / "skill-proposals" / items["pending"].id / "package/SKILL.md"
        path.chmod(0o600)
        path.write_text("Changed frozen candidate")
    rehash(archive)
    target = tmp_path / "restored"
    with pytest.raises((ValueError, RuntimeError, KeyError)) as error:
        backup.restore_backup(archive, target)
    if reason:
        assert str(error.value) == reason
    assert not target.exists()
    assert not list(tmp_path.glob(".application-restore-*"))


def test_failed_scan_pending_candidate_is_retained_without_becoming_installed(source, tmp_path):
    data, plugin, ledger, registry, service, project, run, _ = source
    artifact, _ = write_candidate(
        ledger, project, run, name="unsafe-pending", body="rm -rf /tmp/data"
    )
    candidate = service.submit_artifact(run.id, artifact.id)
    assert not candidate.scan.ok
    archive = make_backup(source, tmp_path)
    target = tmp_path / "restored"
    backup.restore_backup(archive, target)
    restored = SkillProposalService(
        FileLedger(target / "data"),
        SkillRegistry(
            target / "application-plugin",
            target / "data/skills",
        ),
    )
    assert restored.peek(candidate.id) == candidate
    assert all(item.id != candidate.skill_id for item in restored.registry.list())


@pytest.mark.parametrize(
    "location,kind",
    [
        ("source", "symlink"),
        ("source", "hardlink"),
        ("source", "fifo"),
        ("backup", "symlink"),
        ("backup", "hardlink"),
        ("backup", "fifo"),
    ],
)
def test_linked_and_special_files_are_rejected_without_publishing(source, tmp_path, location, kind):
    data, plugin, *_ = source
    archive = make_backup(source, tmp_path) if location == "backup" else None
    root = archive / "payload/data" if archive else data
    path = root / "unexpected"
    if kind == "symlink":
        path.symlink_to(root / "secrets/provider-secrets.json")
    elif kind == "hardlink":
        os.link(root / "secrets/provider-secrets.json", path)
    else:
        os.mkfifo(path)
    target = tmp_path / "target"
    with pytest.raises((ValueError, OSError)):
        if archive:
            backup.restore_backup(archive, target)
        else:
            backup.create_backup(data, plugin, target)
    assert not target.exists()


def test_existing_and_concurrently_created_targets_are_never_overwritten(
    source, tmp_path, monkeypatch
):
    data, plugin, *_ = source
    target = tmp_path / "occupied"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("original")
    with pytest.raises(BackupError, match="target_exists"):
        backup.create_backup(data, plugin, target)
    archive = make_backup(source, tmp_path)
    with pytest.raises(BackupError, match="target_exists"):
        backup.restore_backup(archive, target)
    original_publish = backup.publish_exclusive
    racing = tmp_path / "racing"

    def publish(stage, destination):
        destination.mkdir()
        (destination / "keep").write_text("concurrent")
        original_publish(stage, destination)

    monkeypatch.setattr(backup, "publish_exclusive", publish)
    with pytest.raises(BackupError, match="target_exists"):
        backup.restore_backup(archive, racing)
    assert marker.read_text() == "original"
    assert (racing / "keep").read_text() == "concurrent"


def test_detects_source_changes_and_cleans_incomplete_backup(source, tmp_path, monkeypatch):
    data, plugin, *_ = source
    copy = backup.copy_inventory

    def changing_copy(origin, target, inventory, limits):
        copy(origin, target, inventory, limits)
        if origin == data:
            (data / "added-during-backup").write_text("changed")

    monkeypatch.setattr(backup, "copy_inventory", changing_copy)
    target = tmp_path / "backup"
    with pytest.raises(BackupError, match="source_changed"):
        backup.create_backup(data, plugin, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".application-backup-*"))


def test_bounds_are_enforced_and_manifest_is_not_trusted_for_paths(source, tmp_path):
    data, plugin, *_ = source
    with pytest.raises(BackupError, match="file_count_limit"):
        backup.create_backup(data, plugin, tmp_path / "small", limits=BackupLimits(files=1))
    archive = make_backup(source, tmp_path)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tree"]["files"][0]["path"] = "../../escape"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="file_inventory_mismatch"):
        backup.restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()


def test_combined_data_and_plugin_budget_is_checked_before_copy(source, tmp_path, monkeypatch):
    data, plugin, *_ = source
    count = max(
        len(tree_inventory(root, backup.DEFAULT_LIMITS)["files"]) for root in (data, plugin)
    )

    def no_copy(*args):
        pytest.fail("over-budget snapshot must be rejected before copying")

    monkeypatch.setattr(backup, "copy_inventory", no_copy)
    target = tmp_path / "too-large"
    with pytest.raises(BackupError, match="snapshot_budget_exceeded"):
        backup.create_backup(data, plugin, target, limits=BackupLimits(files=count))
    assert not target.exists()
    assert not list(tmp_path.glob(".application-backup-*"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_maintenance_lock_cannot_alias_a_private_data_file(source, tmp_path, kind):
    data, plugin, *_ = source
    private = data / "secrets/provider-secrets.json"
    before = private.read_bytes()
    lock = data / MAINTENANCE_LOCK
    if kind == "symlink":
        lock.symlink_to(private)
    else:
        os.link(private, lock)
    with pytest.raises((OSError, ValueError)):
        backup.create_backup(data, plugin, tmp_path / "rejected")
    assert private.read_bytes() == before


def _hold_lease(root, exclusive, ready, release):
    with data_directory_lease(Path(root), exclusive=exclusive):
        ready.set()
        release.wait(20)


@pytest.mark.parametrize("exclusive", [False, True])
def test_real_process_leases_exclude_backup_and_application_start(source, tmp_path, exclusive):
    data, plugin, *_ = source
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lease, args=(str(data), exclusive, ready, release))
    process.start()
    try:
        assert ready.wait(15)
        with pytest.raises(DataDirectoryBusyError):
            backup.create_backup(data, plugin, tmp_path / "blocked")
        if exclusive:
            with pytest.raises(DataDirectoryBusyError), data_directory_lease(data):
                pass
        else:
            with data_directory_lease(data):
                pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0
    assert backup.create_backup(data, plugin, tmp_path / "after-release")["published"]


def test_new_process_cli_recovers_without_source_and_keeps_private_values_out_of_output(
    source, tmp_path
):
    data, plugin, *_ = source
    archive = make_backup(source, tmp_path)
    remove_staging(data)
    remove_staging(plugin)
    target = tmp_path / "restored"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ophira_backend.application_backup",
            "restore",
            "--backup",
            str(archive),
            "--output",
            str(target),
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["proposal_states"] == {
        "pending": 2,
        "published": 2,
        "rejected": 1,
    }
    assert "private-backup-canary" not in completed.stdout + completed.stderr
    assert (
        hashlib.sha256((target / "data/secrets/provider-secrets.json").read_bytes()).digest()
        == hashlib.sha256(
            (archive / "payload/data/secrets/provider-secrets.json").read_bytes(),
        ).digest()
    )


@pytest.mark.asyncio
async def test_app_factory_and_entire_lifespan_participate_in_backup_lock(source, tmp_path):
    from test_api import FakeRuntime

    from ophira_backend.config import Settings
    from ophira_backend.main import create_app

    data, plugin, *_ = source
    settings = Settings(_env_file=None, data_dir=data, capability_plugin_dir=plugin)
    with data_directory_lease(data, exclusive=True):
        before = tree_inventory(data, backup.DEFAULT_LIMITS)
        with pytest.raises(DataDirectoryBusyError):
            create_app(settings, runtime=FakeRuntime())
        assert tree_inventory(data, backup.DEFAULT_LIMITS) == before
    app = create_app(settings, runtime=FakeRuntime())
    with data_directory_lease(data, exclusive=True):
        with pytest.raises(DataDirectoryBusyError):
            async with app.router.lifespan_context(app):
                pytest.fail("busy app must not enter startup")
    async with app.router.lifespan_context(app):
        with pytest.raises(DataDirectoryBusyError):
            backup.create_backup(data, plugin, tmp_path / "busy-app")
    assert backup.create_backup(data, plugin, tmp_path / "stopped-app")["published"]


def test_new_empty_application_is_backed_up_without_creating_proposal_state(tmp_path):
    from test_api import FakeRuntime

    from ophira_backend.config import Settings
    from ophira_backend.main import create_app

    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    data = tmp_path / "data"
    app = create_app(
        Settings(_env_file=None, data_dir=data, capability_plugin_dir=plugin), runtime=FakeRuntime()
    )
    before = tree_inventory(data, backup.DEFAULT_LIMITS)
    archive = tmp_path / "backup"
    backup.create_backup(data, plugin, archive)
    assert tree_inventory(data, backup.DEFAULT_LIMITS) == before
    result = backup.restore_backup(archive, tmp_path / "restored")
    assert result["projects"] == result["runs"] == 0
    assert result["proposal_states"] == {}
    assert app.state.ledger.list_projects() == []


def test_application_alias_and_real_root_share_one_lease(source, tmp_path):
    from test_api import FakeRuntime

    from ophira_backend.config import Settings
    from ophira_backend.main import create_app

    data, plugin, *_ = source
    alias = tmp_path / "data-alias"
    alias.symlink_to(data, target_is_directory=True)
    settings = Settings(_env_file=None, data_dir=alias, capability_plugin_dir=plugin)
    with data_directory_lease(data, exclusive=True):
        with pytest.raises(DataDirectoryBusyError):
            create_app(settings, runtime=FakeRuntime())
    app = create_app(settings, runtime=FakeRuntime())
    assert app.state.ledger.root == data.resolve()


def test_recovery_failure_does_not_publish_or_change_backup(source, tmp_path, monkeypatch):
    archive = make_backup(source, tmp_path)
    before = tree_inventory(archive, backup.DEFAULT_LIMITS)
    recover = SkillProposalService.recover

    def unexpected_write(service):
        result = recover(service)
        (service.ledger.root / "secrets/provider-secrets.json").write_text("incorrect change")
        return result

    monkeypatch.setattr(SkillProposalService, "recover", unexpected_write)
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="unexpected_recovery_write"):
        backup.restore_backup(archive, target)
    assert not target.exists()
    assert tree_inventory(archive, backup.DEFAULT_LIMITS) == before
    assert not list(tmp_path.glob(".application-restore-*"))


@pytest.mark.parametrize(
    "change,reason",
    [
        ("bytes", "file_inventory_mismatch"),
        ("code", "implementation_mismatch"),
        ("boolean_schema", "manifest_schema"),
        ("extra_entry", "unexpected_backup_entries"),
        ("logical_inventory", "logical_inventory_mismatch"),
    ],
)
def test_restore_rejects_corrupt_or_incompatible_transport(source, tmp_path, change, reason):
    archive = make_backup(source, tmp_path)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "bytes":
        (archive / "payload/data/secrets/provider-secrets.json").write_text("tampered")
    elif change == "code":
        manifest["implementation"]["main.py"] = "0" * 64
    elif change == "boolean_schema":
        manifest["schema_version"] = True
    elif change == "extra_entry":
        (archive / "unlisted").write_text("must not be ignored")
    else:
        manifest["inventory"]["projects"] = []
    manifest_path.write_text(json.dumps(manifest))
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match=reason):
        backup.restore_backup(archive, target)
    assert not target.exists()


def test_cli_rejects_invalid_private_record_without_printing_contents(source, tmp_path):
    _, _, _, _, _, _, run, _ = source
    archive = make_backup(source, tmp_path)
    path = archive / "payload/data/runs" / run.id / "run.json"
    path.write_text('{"prompt":"private-error-canary","status":"invalid"}')
    rehash(archive)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ophira_backend.application_backup",
            "restore",
            "--backup",
            str(archive),
            "--output",
            str(tmp_path / "restored"),
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "passed": False,
        "reason": "backup_validation_failed",
        "target_visible": False,
    }
    assert "private-error-canary" not in completed.stdout + completed.stderr
