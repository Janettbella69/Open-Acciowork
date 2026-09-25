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
from .plugin_import_backup import inspect_plugin_imports
from .project_comments import MAX_COMMENT_BYTES, CommentLedger
from .project_deletion_proofs import load_deletion_proofs
from .project_template_backup import inspect_project_templates
from .skill_proposal_files import directory_fd, read_relative, relative_parts
from .skill_proposals import SkillProposalService
from .skill_repository_backup import inspect_skill_imports
from .skills import CONTENT_HASH_PATTERN, TENANT_PLUGIN_NAMESPACE, SkillRegistry
from .storage import FileLedger, LedgerNotFoundError
from .workspace_checkpoints import (
    LIMITS as WORKSPACE_LIMITS,
)
from .workspace_checkpoints import (
    MAX_ARCHIVED_RESTORES,
    MAX_CHECKPOINTS,
    MAX_RESTORES,
    PENDING_ARCHIVE,
    PENDING_RESTORE,
    PENDING_RETENTION,
    WorkspaceCheckpoints,
    WorkspaceRestore,
)
from .workspace_checkpoints import (
    fingerprint as workspace_fingerprint,
)

FORMAT = "ophira-application-cold-backup"
DEFAULT_LIMITS = BackupLimits()
DELETION_LIMITS = BackupLimits(
    files=20_000, entries=40_000, total_bytes=512 * 1024 * 1024,
)
MAX_MEMORY_TOMBSTONES = 100_000


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
    _deletion_register(data)
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
        if ((directory / PENDING_RESTORE).exists()
                or (directory / PENDING_RETENTION).exists()
                or (directory / PENDING_ARCHIVE).exists()
                or list(directory.glob("workspace-stage-*"))):
            # Pending swaps bind actual directory inodes. Finish them on the
            # original host before copying; never replay them in a cold restore.
            raise BackupError("pending_workspace_restore")
        try:
            from .artifact_files import read_snapshot

            for identifier in checkpoints._ids(
                project.id,
                "workspace-checkpoints",
                MAX_CHECKPOINTS,
            ):
                item = checkpoints.load(project.id, identifier)
                for row in item.files:
                    if len(read_snapshot(directory, row.sha256)) != row.size:
                        raise ValueError("snapshot size mismatch")
            active_reviews = checkpoints._ids(project.id, "workspace-restores", MAX_RESTORES)
            for identifier in active_reviews:
                item = checkpoints.get_restore(project.id, identifier)
                if item.archived_at is not None:
                    raise ValueError("active recovery review is archived")
                target = checkpoints.load(project.id, item.checkpoint_id)
                if target.fingerprint != item.target_fingerprint or item.state == "applying":
                    raise ValueError("invalid recovery target")
                if item.state == "applied":
                    safety = checkpoints.load(project.id, item.safety_checkpoint_id or "")
                    if safety.fingerprint != item.current_fingerprint:
                        raise ValueError("invalid recovery safety copy")
                    retained = directory / f"workspace-retained-{item.id}"
                    if item.retained_discarded_at is None:
                        if (not retained.is_dir()
                                or workspace_fingerprint(tree_inventory(
                                    retained, WORKSPACE_LIMITS,
                                )) != safety.fingerprint
                                or item.retained_bytes != safety.total_bytes):
                            raise ValueError("invalid retained workspace copy")
                    elif (retained.exists() or retained.is_symlink()
                          or item.retained_bytes != 0):
                        raise ValueError("discarded workspace copy still present")
            for identifier in checkpoints._ids(
                project.id, "workspace-restores-archive", MAX_ARCHIVED_RESTORES,
            ):
                item = checkpoints._read(
                    project.id, "workspace-restores-archive", identifier, WorkspaceRestore,
                )
                if (identifier in active_reviews or item.state != "applied"
                        or item.archived_at is None or item.archived_by is None
                        or item.retained_discarded_at is None or item.retained_bytes != 0
                        or (directory / f"workspace-retained-{identifier}").exists()
                        or (directory / f"workspace-retained-{identifier}").is_symlink()):
                    raise ValueError("invalid archived recovery review")
        except (LedgerNotFoundError, OSError, ValueError) as exc:
            raise BackupError("workspace_checkpoint_invalid") from exc
        for artifact in project.artifacts:
            if artifact.content_storage == "snapshot":
                try:
                    ledger.capture_artifact(project.id, artifact.id)
                except (OSError, ValueError) as exc:
                    raise BackupError("artifact_snapshot_invalid") from exc
        projects[project.id] = project
    inspect_project_templates(data, projects)
    comment_store = data / "collaboration" / "comments"
    if comment_store.exists():
        for path in sorted(comment_store.iterdir()):
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                continue  # Uncommitted atomic-write staging is not discussion history.
            try:
                record = CommentLedger.model_validate_json(
                    read_relative(
                        data,
                        f"collaboration/comments/{path.name}",
                        MAX_COMMENT_BYTES,
                    )
                )
                project = projects[record.project_id]
                if (
                    path.name != f"{project.id}.json"
                    or record.tenant_id != project.tenant_id
                    or record.identity_binding != project.identity_binding
                ):
                    raise ValueError("comment scope mismatch")
            except (OSError, ValueError, KeyError) as exc:
                raise BackupError("project_comments_invalid") from exc
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
    from .conversation_deletion_state import ConversationDeletionStates

    run_origins = dict(runs)
    for project_id in projects:
        origins = ConversationDeletionStates(ledger.lifecycle).origins(project_id)
        for identifier, origin in origins.items():
            if identifier in runs:
                raise BackupError("deleted_conversation_run_reappeared")
            run_origins[identifier] = origin
    items = [proposals.peek(identifier) for identifier in proposals._ids(clean_staging=False)]
    by_id = {item.id: item for item in items}
    for item in items:
        proposals._verified_files(item)
        if [row.id for row in item.source_runs] != item.manifest.related_run_ids:
            raise BackupError("proposal_related_run_mismatch")
        run = run_origins.get(item.source_run_id)
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
            related = run_origins.get(source_run.id)
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
    imports = inspect_skill_imports(ledger, registry)
    plugin_imports = inspect_plugin_imports(ledger, imports)
    try:
        deleted_publications = {}
        for proof in load_deletion_proofs(data).values():
            for receipt in proof.publications:
                if receipt.proposal_id in deleted_publications:
                    raise ValueError("duplicate deleted publication proof")
                deleted_publications[receipt.proposal_id] = receipt
    except (OSError, ValueError) as exc:
        raise BackupError("project_deletion_proof_invalid") from exc
    for proposal_id, receipt in canonical.get("publication_receipts", {}).items():
        deleted_receipt = deleted_publications.get(proposal_id)
        if deleted_receipt is not None and deleted_receipt.model_dump(mode="json") != receipt:
            raise BackupError("deleted_publication_receipt_mismatch")
        if proposal_id not in by_id and proposal_id not in imports and deleted_receipt is None:
            raise BackupError("publication_proposal_missing")
        namespace, name = receipt["skill_id"].split(":", 1)
        registry._skill_from_history_unlocked(
            namespace, name, receipt["content_hash"], enabled=False
        )
    if set(deleted_publications) - set(canonical.get("publication_receipts", {})):
        raise BackupError("deleted_publication_receipt_missing")
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
        "skill_imports": {
            item.id: {
                "state": item.state,
                "revision": item.revision,
                "content_hash": item.skill.content_hash,
            }
            for item in imports.values()
        },
        "plugin_imports": {
            item.id: {
                "archive_hash": item.archive_hash,
                "skill_imports": [skill.import_id for skill in item.skills],
            }
            for item in plugin_imports.values()
        },
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


def _memory_deletion_register(data: Path) -> dict:
    """Capture content-free tombstones and reject unfinished memory scrubs."""
    from .constants import MemoryScope
    from .models import MemoryTombstone

    root = data / "memory"
    if not root.exists() and not root.is_symlink():
        return {"files": [], "tombstones": 0}
    inventory = tree_inventory(root, DEFAULT_LIMITS)
    files = {row["path"]: row for row in inventory["files"]}
    staging_scopes = set()
    for candidate in files:
        directory, _, name = candidate.rpartition("/")
        if name in {"items.json.tmp", "history.jsonl.tmp"} or name.startswith((
            ".ophira-memory-items.json-", ".ophira-memory-history.jsonl-",
        )):
            staging_scopes.add(directory)
    tombstone_files = []
    total = 0
    seen_ids = set()
    for path, row in files.items():
        if path.rsplit("/", 1)[-1] != "tombstones.jsonl":
            continue
        parts = relative_parts(path)
        if len(parts) != 3 or parts[0] not in {scope.value for scope in MemoryScope}:
            raise BackupError("memory_deletion_register_invalid")
        raw = read_relative(root, path, DEFAULT_LIMITS.file_bytes)
        if not raw.endswith(b"\n") or raw.count(b"\n") + total > MAX_MEMORY_TOMBSTONES:
            raise BackupError("memory_deletion_register_invalid")
        deleted_ids = set()
        try:
            for line in raw.splitlines():
                tombstone = MemoryTombstone.model_validate_json(line)
                if (
                    tombstone.scope.value != parts[0]
                    or tombstone.scope_id != parts[1]
                    or tombstone.memory_id in seen_ids
                ):
                    raise ValueError("memory deletion scope or identity mismatch")
                deleted_ids.add(tombstone.memory_id)
                seen_ids.add(tombstone.memory_id)
        except ValueError:
            raise BackupError("memory_deletion_register_invalid") from None
        total += len(deleted_ids)
        prefix = f"{parts[0]}/{parts[1]}/"
        if prefix[:-1] in staging_scopes:
            raise BackupError("memory_deletion_scrub_incomplete")
        items_path = prefix + "items.json"
        if items_path in files:
            try:
                items = parse_json(read_relative(root, items_path, DEFAULT_LIMITS.file_bytes))
                if not isinstance(items, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    for item in items
                ):
                    raise ValueError("invalid memory items")
            except ValueError:
                raise BackupError("memory_deletion_register_invalid") from None
            if any(item["id"] in deleted_ids for item in items):
                raise BackupError("memory_deletion_scrub_incomplete")
        history_path = prefix + "history.jsonl"
        if history_path in files:
            history_deleted = False
            try:
                history = read_relative(root, history_path, DEFAULT_LIMITS.file_bytes)
                for line in history.splitlines():
                    entry = parse_json(line)
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("item"), dict)
                        or not isinstance(entry["item"].get("id"), str)
                    ):
                        raise ValueError("invalid memory history")
                    if entry["item"].get("id") in deleted_ids:
                        history_deleted = True
            except ValueError:
                raise BackupError("memory_deletion_register_invalid") from None
            if history_deleted:
                raise BackupError("memory_deletion_scrub_incomplete")
        if parts[0] == MemoryScope.AGENT.value:
            agent_directory = data / "agents" / parts[1]
            if agent_directory.exists() or agent_directory.is_symlink():
                with directory_fd(agent_directory) as descriptor:
                    names = os.listdir(descriptor)
                if (
                    len(names) > DEFAULT_LIMITS.entries
                    or any(name.startswith(".ophira-memory-MEMORY.md-") for name in names)
                ):
                    raise BackupError("memory_deletion_scrub_incomplete")
            try:
                mirror = read_relative(
                    data, f"agents/{parts[1]}/MEMORY.md", DEFAULT_LIMITS.file_bytes,
                )
            except FileNotFoundError:
                mirror = b""
            if any(f"<!-- mem:{identifier} -->".encode() in mirror for identifier in deleted_ids):
                raise BackupError("memory_deletion_scrub_incomplete")
        tombstone_files.append(row)
    return {"files": tombstone_files, "tombstones": total}


def _deletion_register(data: Path) -> dict:
    """Read committed project, conversation, and memory deletion records."""
    from .conversation_deletion_state import ConversationDeletionStates
    from .project_deletion_proofs import deleted_project_ids
    from .project_lifecycle import ProjectLifecycle

    with directory_fd(data):
        pass
    lifecycle = ProjectLifecycle(data)
    conversations = ConversationDeletionStates(lifecycle)
    inventories = {}
    for name in ("project-deletions", "conversation-deletions"):
        path = data / name
        if path.exists() or path.is_symlink():
            inventories[name] = tree_inventory(path, DELETION_LIMITS)
        else:
            inventories[name] = {"files": [], "directories": []}
    if lifecycle.pending_ids() or conversations.pending():
        raise BackupError("pending_deletion_reconciliation")
    project_ids = deleted_project_ids(data)
    if set(load_deletion_proofs(data)) != project_ids:
        raise BackupError("project_deletion_register_invalid")
    project_inventory = inventories["project-deletions"]
    if (
        {row["path"] for row in project_inventory["directories"]} != project_ids
        or {row["path"] for row in project_inventory["files"]}
        != {f"{identifier}/{name}" for identifier in project_ids
            for name in ("state.json", "proofs.json")}
    ):
        raise BackupError("project_deletion_register_invalid")
    conversation_count = 0
    expected_files = set()
    for row in inventories["conversation-deletions"]["directories"]:
        if "/" in row["path"]:
            raise BackupError("conversation_deletion_register_invalid")
        project_id = row["path"]
        states = conversations.list(project_id)
        if any(state["status"] != "deleted" for state in states):
            raise BackupError("pending_deletion_reconciliation")
        conversations.origins(project_id)
        conversation_count += len(states)
        expected_files.update(
            f"{project_id}/{state['operation_id']}.json" for state in states
        )
    if {row["path"] for row in inventories["conversation-deletions"]["files"]} != expected_files:
        raise BackupError("conversation_deletion_register_invalid")
    return {
        "inventory": inventories,
        "project_deletions": len(project_ids),
        "conversation_deletions": conversation_count,
        "memory": _memory_deletion_register(data),
    }


def _restore_backup(
    backup: Path,
    target: Path,
    *,
    current_data_dir: Path | None,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> dict:
    sources = (backup,) if current_data_dir is None else (backup, current_data_dir)
    target = _new_target(target, *sources)
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
        reconciliation = None
        if current_data_dir is not None:
            current = _deletion_register(current_data_dir)
            recovered = _deletion_register(restored / "data")
            if (
                current["inventory"] != recovered["inventory"]
                or current["memory"]["files"] != recovered["memory"]["files"]
            ):
                raise BackupError("deletion_register_mismatch")
            reconciliation = {
                "scope": "project_conversation_memory",
                "project_deletions": current["project_deletions"],
                "conversation_deletions": current["conversation_deletions"],
                "memory_deletions": current["memory"]["tombstones"],
            }
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
            "deletion_reconciliation": reconciliation,
        }
    finally:
        # After publication stage contains no target. Failure never removes a
        # target created by another process or overwrites an existing deployment.
        remove_staging(stage)


def restore_backup(
    backup: Path,
    target: Path,
    *,
    current_data_dir: Path | None = None,
    acknowledge_unreconciled_history: bool = False,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> dict:
    """Restore with the latest deletion register or explicit operator acknowledgement."""
    if (current_data_dir is None) != acknowledge_unreconciled_history:
        raise BackupError("deletion_reconciliation_choice_required")
    if current_data_dir is None:
        return _restore_backup(backup, target, current_data_dir=None, limits=limits)
    current_data_dir = current_data_dir.absolute()
    backup = backup.absolute()
    if current_data_dir.is_relative_to(backup) or backup.is_relative_to(current_data_dir):
        raise BackupError("overlapping_paths")
    with data_directory_lease(current_data_dir, exclusive=True):
        return _restore_backup(
            backup, target, current_data_dir=current_data_dir, limits=limits,
        )


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
    restore.add_argument("--current-data-dir", type=Path)
    restore.add_argument("--acknowledge-unreconciled-history", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            create_backup(args.data_dir, args.plugin_dir, args.output)
            if args.mode == "create"
            else restore_backup(
                args.backup, args.output,
                current_data_dir=args.current_data_dir,
                acknowledge_unreconciled_history=args.acknowledge_unreconciled_history,
            )
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
