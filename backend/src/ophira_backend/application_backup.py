"""Offline application data snapshots; no Settings, HTTP app or model runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from .backup_files import (
    BackupError,
    BackupLimits,
    copy_inventory,
    parse_json,
    private_json,
    publish_exclusive,
    remove_staging,
    sync_directories,
    tree_inventory,
)
from .constants import TERMINAL_RUN_STATUSES
from .maintenance import MAINTENANCE_LOCK, DataDirectoryBusyError, data_directory_lease
from .skill_proposal_files import directory_fd, read_relative
from .skill_proposals import SkillProposalService
from .skills import CONTENT_HASH_PATTERN, TENANT_PLUGIN_NAMESPACE, SkillRegistry
from .storage import FileLedger, LedgerNotFoundError
from .workspace_checkpoints import (
    MAX_CHECKPOINTS,
    MAX_RESTORES,
    PENDING_RESTORE,
    WorkspaceCheckpoints,
)

FORMAT = "ophira-application-cold-backup"
DEFAULT_LIMITS = BackupLimits()


def implementation() -> dict[str, str]:
    root = Path(__file__).parent
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*.py"))
    }


def _new_target(target: Path, *sources: Path) -> Path:
    target = target.absolute()
    with directory_fd(target.parent):
        pass
    if target.exists() or target.is_symlink():
        raise BackupError("target_exists")
    for source in sources:
        source = source.absolute()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise BackupError("overlapping_paths")
    return target


def inspect_application(data: Path, plugin: Path) -> dict:
    """Validate a private copy without repairing it or consulting current grants."""
    if not (data / "skills/registry-state.json").is_file():
        raise BackupError("canonical_state_missing")
    for name in ("projects", "runs", "skill-proposals"):
        if not (data / name).is_dir():
            raise BackupError("application_store_missing")
    ledger = FileLedger(data)
    registry = SkillRegistry(plugin, data / "skills")
    proposals = SkillProposalService(ledger, registry)
    # list_projects/list_runs deliberately hide malformed UI records. A backup
    # must instead read every directory strictly so missing truth cannot vanish.
    projects, runs = {}, {}
    checkpoints = WorkspaceCheckpoints(ledger)
    for directory in sorted(ledger.projects_dir.iterdir()):
        if not directory.is_dir():
            raise BackupError("invalid_project_store")
        project = ledger.get_project(directory.name)
        if project.id != directory.name:
            raise BackupError("project_identity_mismatch")
        if (directory / PENDING_RESTORE).exists() or list(directory.glob("workspace-stage-*")):
            # Pending swaps bind actual directory inodes. Finish them on the
            # original host before copying; never replay them in a cold restore.
            raise BackupError("pending_workspace_restore")
        try:
            from .artifact_files import read_snapshot

            for identifier in checkpoints._ids(
                project.id, "workspace-checkpoints", MAX_CHECKPOINTS,
            ):
                item = checkpoints.load(project.id, identifier)
                for row in item.files:
                    if len(read_snapshot(directory, row.sha256)) != row.size:
                        raise ValueError("snapshot size mismatch")
            for identifier in checkpoints._ids(project.id, "workspace-restores", MAX_RESTORES):
                item = checkpoints.get_restore(project.id, identifier)
                target = checkpoints.load(project.id, item.checkpoint_id)
                if target.fingerprint != item.target_fingerprint or item.state == "applying":
                    raise ValueError("invalid recovery target")
                if item.state == "applied":
                    safety = checkpoints.load(project.id, item.safety_checkpoint_id or "")
                    if safety.fingerprint != item.current_fingerprint:
                        raise ValueError("invalid recovery safety copy")
        except (LedgerNotFoundError, OSError, ValueError) as exc:
            raise BackupError("workspace_checkpoint_invalid") from exc
        for artifact in project.artifacts:
            if artifact.content_storage == "snapshot":
                try:
                    ledger.capture_artifact(project.id, artifact.id)
                except (OSError, ValueError) as exc:
                    raise BackupError("artifact_snapshot_invalid") from exc
        projects[project.id] = project
    for directory in sorted(ledger.runs_dir.iterdir()):
        if not directory.is_dir():
            raise BackupError("invalid_run_store")
        run = ledger.get_run(directory.name)
        if run.id != directory.name or run.project_id not in projects:
            raise BackupError("run_project_mismatch")
        project = projects[run.project_id]
        if run.tenant_id != project.tenant_id or run.identity_binding != project.identity_binding:
            raise BackupError("run_identity_mismatch")
        if run.status not in TERMINAL_RUN_STATUSES:
            raise BackupError("nonterminal_run_present")
        runs[run.id] = run
    with registry._exclusive():
        canonical = registry._canonical_state_unlocked()
        packages = 0
        for namespace in sorted(registry.versions_dir.iterdir()):
            if namespace.name not in {registry._namespace(), TENANT_PLUGIN_NAMESPACE}:
                raise BackupError("unknown_skill_namespace")
            for name in sorted(namespace.iterdir()):
                for revision in sorted(name.iterdir()):
                    if not CONTENT_HASH_PATTERN.fullmatch(revision.name):
                        raise BackupError("invalid_skill_history")
                    item = registry._skill_from_history_unlocked(
                        namespace.name,
                        name.name,
                        revision.name,
                        enabled=False,
                    )
                    if not registry._test_skill(item).ok:
                        raise BackupError("history_scan_failed")
                    packages += 1
        for item in registry._list_unlocked():
            if not registry._test_skill(item).ok:
                raise BackupError("current_skill_scan_failed")
        for run in runs.values():
            registry._normalize_snapshot_unlocked(run.skill_snapshot)
    # This is a private, static snapshot: do not create a new ledger lock merely
    # to inspect a store which has never received a proposal.
    items = [proposals.peek(identifier) for identifier in proposals._ids(clean_staging=False)]
    by_id = {item.id: item for item in items}
    for item in items:
        proposals._verified_files(item)
        if [row.id for row in item.source_runs] != item.manifest.related_run_ids:
            raise BackupError("proposal_related_run_mismatch")
        run = runs.get(item.source_run_id)
        if (
            run is None
            or run.project_id != item.project_id
            or run.started_at is None
            or run.sdk_session_id != item.source_sdk_session_id
        ):
            raise BackupError("proposal_run_mismatch")
        project = projects[item.project_id]
        artifact = next(
            (row for row in project.artifacts if row.id == item.source_artifact_id), None
        )
        if (
            artifact is None
            or artifact.project_id != project.id
            or artifact.run_id != run.id
            or artifact.checksum != item.manifest_checksum
        ):
            raise BackupError("proposal_artifact_mismatch")
        for source_run in item.source_runs:
            related = runs.get(source_run.id)
            if (
                related is None
                or related.project_id != project.id
                or related.id == run.id
                or related.started_at != source_run.started_at
            ):
                raise BackupError("proposal_related_run_mismatch")
        receipt = registry.publication_receipt(item.id)
        if item.state == "published" and receipt != item.publication_receipt:
            raise BackupError("proposal_publication_receipt_missing")
        if receipt is not None and (
            item.state not in {"published", "publishing"}
            or not proposals._receipt_matches(item, receipt)
        ):
            raise BackupError("proposal_publication_receipt_mismatch")
    for proposal_id, receipt in canonical.get("publication_receipts", {}).items():
        if proposal_id not in by_id:
            raise BackupError("publication_proposal_missing")
        namespace, name = receipt["skill_id"].split(":", 1)
        registry._skill_from_history_unlocked(
            namespace, name, receipt["content_hash"], enabled=False
        )
    return {
        "projects": sorted(projects),
        "runs": sorted(runs),
        "historical_packages": packages,
        "proposals": {
            item.id: {
                "state": item.state,
                "revision": item.revision,
                "content_hash": item.content_hash,
            }
            for item in items
        },
        "canonical_state": canonical,
    }


def _bounded_inventory(root: Path, limits: BackupLimits) -> dict:
    result = tree_inventory(root, limits)
    if {row["path"].split("/", 1)[0] for row in result["directories"]} != {
        "data",
        "application-plugin",
    } or any(
        row["path"].split("/", 1)[0] not in {"data", "application-plugin"}
        for row in result["files"]
    ):
        raise BackupError("invalid_payload_roots")
    return result


def _check_recovery_changes(before: dict, after: dict, logical: dict) -> None:
    allowed = {
        f"data/skill-proposals/{identifier}/record.json"
        for identifier, item in logical["proposals"].items()
        if item["state"] == "publishing"
    }
    lock = f"data/.ledger-locks/{hashlib.sha256(b'skill-proposals').hexdigest()}.lock"
    for category in ("files", "directories"):
        old = {row["path"]: row for row in before[category]}
        new = {row["path"]: row for row in after[category]}
        for path in old.keys() | new.keys():
            if old.get(path) == new.get(path):
                continue
            if category == "files" and path in allowed and path in old and path in new:
                continue
            if (
                category == "files"
                and path == lock
                and path not in old
                and new[path]["size"] == 0
                and new[path]["sha256"] == hashlib.sha256(b"").hexdigest()
            ):
                continue
            if path.startswith("data/skill-proposals/.staging-") and path not in new:
                continue
            raise BackupError("unexpected_recovery_write")


def create_backup(
    data: Path,
    plugin: Path,
    target: Path,
    *,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> dict:
    target = _new_target(target, data, plugin)
    before_code = implementation()
    with data_directory_lease(data, exclusive=True):
        initial_data = tree_inventory(data, limits, omit_root=frozenset({MAINTENANCE_LOCK}))
        initial_plugin = tree_inventory(plugin, limits)
        all_files = initial_data["files"] + initial_plugin["files"]
        entry_count = len(all_files) + len(initial_data["directories"])
        entry_count += len(initial_plugin["directories"]) + 2
        if (
            len(all_files) > limits.files
            or entry_count > limits.entries
            or sum(row["size"] for row in all_files) > limits.total_bytes
        ):
            raise BackupError("snapshot_budget_exceeded")
        stage = Path(tempfile.mkdtemp(prefix=".application-backup-", dir=target.parent))
        published = False
        try:
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            copy_inventory(data, payload / "data", initial_data, limits)
            copy_inventory(plugin, payload / "application-plugin", initial_plugin, limits)
            tree = _bounded_inventory(payload, limits)
            logical = inspect_application(payload / "data", payload / "application-plugin")
            if tree != _bounded_inventory(payload, limits):
                raise BackupError("inspection_changed_snapshot")
            if initial_data != tree_inventory(
                data, limits, omit_root=frozenset({MAINTENANCE_LOCK})
            ) or initial_plugin != tree_inventory(plugin, limits):
                raise BackupError("source_changed")
            if implementation() != before_code:
                raise BackupError("implementation_changed")
            manifest = {
                "schema_version": 1,
                "format": FORMAT,
                "implementation": before_code,
                "tree": tree,
                "inventory": logical,
            }
            private_json(stage / "manifest.json", manifest)
            if (stage / "manifest.json").stat().st_size > limits.manifest_bytes:
                raise BackupError("manifest_byte_limit")
            sync_directories(stage)
            publish_exclusive(stage, target)
            published = True
            with directory_fd(target.parent) as descriptor:
                os.fsync(descriptor)
            return {
                "published": True,
                "files": len(tree["files"]),
                "bytes": sum(row["size"] for row in tree["files"]),
                "projects": len(logical["projects"]),
                "runs": len(logical["runs"]),
                "proposals": len(logical["proposals"]),
            }
        finally:
            if not published:
                remove_staging(stage)


def restore_backup(
    backup: Path,
    target: Path,
    *,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> dict:
    target = _new_target(target, backup)
    raw = read_relative(backup, "manifest.json", limits.manifest_bytes)
    manifest = parse_json(raw)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "format", "implementation", "tree", "inventory"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["format"] != FORMAT
    ):
        raise BackupError("manifest_schema")
    if manifest["implementation"] != implementation():
        raise BackupError("implementation_mismatch")
    with directory_fd(backup) as descriptor:
        if set(os.listdir(descriptor)) != {"manifest.json", "payload"}:
            raise BackupError("unexpected_backup_entries")
    actual = _bounded_inventory(backup / "payload", limits)
    if actual != manifest["tree"]:
        raise BackupError("file_inventory_mismatch")
    stage = Path(tempfile.mkdtemp(prefix=".application-restore-", dir=target.parent))
    try:
        # copy_inventory creates its destination exclusively; publication uses
        # this nested complete root so no partially restored data becomes live.
        restored = stage / "restored"
        copy_inventory(backup / "payload", restored, actual, limits)
        logical = inspect_application(restored / "data", restored / "application-plugin")
        if logical != manifest["inventory"]:
            raise BackupError("logical_inventory_mismatch")
        if _bounded_inventory(restored, limits) != actual:
            raise BackupError("inspection_changed_snapshot")
        ledger = FileLedger(restored / "data")
        registry = SkillRegistry(restored / "application-plugin", restored / "data/skills")
        service = SkillProposalService(ledger, registry)
        # Reconcile only reviewed publication intents on the private copy. No
        # application lifespan, schedule, notification, SDK session or tool runs.
        canonical_before = registry.registry_state_path.read_bytes()
        recovery = service.recover()
        if recovery.failed or registry.registry_state_path.read_bytes() != canonical_before:
            raise BackupError("proposal_recovery_failed")
        after = inspect_application(restored / "data", restored / "application-plugin")
        _check_recovery_changes(actual, _bounded_inventory(restored, limits), logical)
        states = Counter(row["state"] for row in after["proposals"].values())
        if manifest["implementation"] != implementation():
            raise BackupError("implementation_changed")
        if read_relative(backup, "manifest.json", limits.manifest_bytes) != raw:
            raise BackupError("backup_changed")
        sync_directories(restored)
        publish_exclusive(restored, target)
        with directory_fd(target.parent) as descriptor:
            os.fsync(descriptor)
        return {
            "published": True,
            "files": len(actual["files"]),
            "projects": len(after["projects"]),
            "runs": len(after["runs"]),
            "proposal_states": dict(states),
            "proposal_recovery": recovery.model_dump(),
            "canonical_preserved": True,
            "provider_invoked": False,
        }
    finally:
        # After publication stage contains no target. Failure never removes a
        # target created by another process or overwrites an existing deployment.
        remove_staging(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    create = modes.add_parser(
        "create", help="Back up a stopped application's complete data directory"
    )
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--plugin-dir", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    restore = modes.add_parser(
        "restore", help="Restore to a new private directory; never starts jobs"
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            create_backup(args.data_dir, args.plugin_dir, args.output)
            if args.mode == "create"
            else restore_backup(args.backup, args.output)
        )
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        reason = (
            str(error)
            if isinstance(error, BackupError)
            else "data_directory_busy"
            if isinstance(error, DataDirectoryBusyError)
            else "backup_validation_failed"
        )
        print(
            json.dumps({"passed": False, "reason": reason, "target_visible": args.output.exists()})
        )
        return 1
    print(json.dumps({"passed": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
