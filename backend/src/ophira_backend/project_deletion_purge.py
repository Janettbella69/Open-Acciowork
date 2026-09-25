"""Idempotent application erasure after callers stop workers and hold lifecycle leases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .backup_files import parse_json
from .project_deletion_capture import (
    JSON_LIMIT,
    DeletionCapture,
    ProjectDeletionConflict,
    PurgePlan,
    capture_companion_staging,
    diary_selection,
)
from .project_deletion_proofs import proof_hash
from .project_deletion_shared import shared_file_lease
from .skill_proposal_files import child_directory, directory_fd, read_relative, relative_parts


def _atomic_bytes(root: Path, relative: str, raw: bytes):
    parts = relative_parts(relative)
    with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
        temporary = "." + parts[-1] + "." + uuid.uuid4().hex + ".tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _json_bytes(value):
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()


@contextmanager
def _existing_store_lock(root, path):
    """Use exact existing store locks; never unlink their coordinating inode."""
    import fcntl

    parts = relative_parts(path)
    with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
        descriptor = os.open(
            parts[-1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ProjectDeletionConflict("Shared store lock is not a private regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProjectDeletionConflict(
                    "A shared project store is busy; retry erasure",
                ) from exc
            yield
        finally:
            os.close(descriptor)


def _rewrite(root, plan, rewrite):
    if rewrite.kind in {"manifest", "run-manifest"}:
        lock = _existing_store_lock(
            root, ".ledger-locks/" + hashlib.sha256(b"manifest").hexdigest() + ".lock"
        )
    elif rewrite.kind in {"pairing", "push"}:
        lock = _existing_store_lock(root, rewrite.path.split("/", 1)[0] + "/.transaction.lock")
    else:
        lock = shared_file_lease(root, rewrite.path)
    with ExitStack() as stack:
        if rewrite.kind in {"pairing", "push"} and (root / "companion-push").is_dir():
            pairing_id = rewrite.path.rsplit("/", 1)[1].removesuffix(".json")
            stack.enter_context(_existing_store_lock(root, f"companion-push/{pairing_id}.lock"))
        stack.enter_context(lock)
        try:
            raw = read_relative(root, rewrite.path, max(JSON_LIMIT, 64 * 1024 * 1024))
        except FileNotFoundError:
            return
        if rewrite.kind in {"manifest", "run-manifest"}:
            rows = [parse_json(line) for line in raw.splitlines() if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise ProjectDeletionConflict("Invalid manifest during project deletion")
            def matches(row):
                return (row.get("run_id") in rewrite.selectors if rewrite.kind == "run-manifest"
                        else row.get("project_id") == plan.project_id)

            selected = [row for row in rows if matches(row)]
            updated = b"".join(
                _json_bytes(row) for row in rows if not matches(row)
            )
        elif rewrite.kind == "diary":
            selected, updated, _blocks = diary_selection(
                raw,
                set(rewrite.selectors),
                blocks=rewrite.diary_blocks,
            )
        elif rewrite.kind == "pairing":
            from .companion import PairingRecord, PairingStatus
            from .models import utc_now

            record = PairingRecord.model_validate_json(raw)
            if rewrite.path != f"companion-devices/{record.id}.json":
                raise ProjectDeletionConflict("Companion pairing identity mismatch")
            if plan.project_id not in record.project_ids:
                return
            record.project_ids = [item for item in record.project_ids if item != plan.project_id]
            if not record.project_ids:
                record.status = PairingStatus.REVOKED
                record.revoked_at = utc_now()
                record.link_secret_hash = None
                record.cookie_secret_hash = None
                record.cookie_activated = False
            _atomic_bytes(root, rewrite.path, _json_bytes(record.model_dump(mode="json")))
            return
        else:
            from .companion import PairingRecord
            from .models import new_id
            from .push_store import PushRecord

            record = PushRecord.model_validate_json(raw)
            pairing = PairingRecord.model_validate_json(
                read_relative(root, f"companion-devices/{record.id}.json", JSON_LIMIT)
            )
            anchors = set(rewrite.selectors)
            record.known = {key: value for key, value in record.known.items() if key not in anchors}
            if record.wake:
                record.wake.anchors = [key for key in record.wake.anchors if key not in anchors]
                if not record.wake.anchors:
                    record.wake.state = "cancelled"
                    record.wake.reason = "project_deleted"
            if not pairing.project_ids:
                record.subscription = None
                record.disabled_reason = "device_authority_revoked"
                record.known = {}
                record.wake = None
            record.generation = new_id("push")
            _atomic_bytes(root, rewrite.path, _json_bytes(record.model_dump(mode="json")))
            return
        if not selected:
            return
        if proof_hash(selected) != rewrite.selected_hash:
            raise ProjectDeletionConflict("Reviewed shared project content changed before erasure")
        _atomic_bytes(root, rewrite.path, updated)


def _validate_plan_paths(plan):
    project = plan.project_id
    if plan.proofs.project_id != project:
        raise ProjectDeletionConflict("Deletion provenance has a different project scope")
    if len({item.path for item in plan.entries}) != len(plan.entries):
        raise ProjectDeletionConflict("Deletion plan contains duplicate sources")
    for identifier in plan.companion_ids:
        if not re.fullmatch(r"pair_[a-f0-9]{32}", identifier):
            raise ProjectDeletionConflict("Invalid Companion deletion identity")
    allowed_trees = {
        f"projects/{project}",
        f"receipts/{project}",
        f"commercial-actions/{project}",
        f"auth/projects/{project}",
        f"memory/project/{project}",
        f"browser-profiles/{project}",
        f"collaboration/project-templates/{project}",
        f"secrets/event-triggers/{project}",
    }
    for root, identifiers in (
        ("runs", plan.run_ids),
        ("schedules", plan.schedule_ids),
        ("shop-browsers", plan.shop_ids),
    ):
        for identifier in identifiers:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                raise ProjectDeletionConflict("Invalid deletion source identity")
            allowed_trees.add(root + "/" + identifier)
    for entry in plan.entries:
        if entry.path.startswith("skill-proposals/"):
            parts = relative_parts(entry.path)
            if len(parts) >= 3 and re.fullmatch(r"skill_proposal_[a-f0-9]{64}", parts[1]):
                allowed_trees.add("/".join(parts[:2]))
    application_files = set()
    for proof in plan.proofs.templates:
        family = (
            f"collaboration/project-templates/{proof.source_project_id}/"
            f"{proof.template_id}/applications"
        )
        application_files.add(family + "/" + proof.application_id + ".json")
        allowed_trees.add(family + "/." + proof.application_id + ".stage")
    if not set(plan.trees) <= allowed_trees:
        raise ProjectDeletionConflict("Deletion plan contains an unowned directory")
    for directory in plan.directories:
        if not any(directory == tree or directory.startswith(tree + "/") for tree in allowed_trees):
            raise ProjectDeletionConflict("Deletion plan contains an unowned directory")
    for entry in plan.entries:
        path = entry.path
        if any(path.startswith(tree + "/") for tree in allowed_trees):
            continue
        parts = relative_parts(path)
        flat = len(parts) == 2 and parts[0] in {
            "intents",
            "notifications",
            "grants",
            "feedback",
            "channel-deliveries",
            "shop-browsers",
            "task-experiences",
            "companion-devices",
            "companion-push",
        }
        fixed = path in {
            f"auth/marketplaces/{project}.json",
            f"auth/gmail/{project}.json",
            f"collaboration/comments/{project}.json",
        }
        temporary = (
            len(parts) == 3
            and "/".join(parts[:2]) in {
                "auth/marketplaces", "auth/gmail", "collaboration/comments",
            }
            and parts[-1].startswith(f".{project}.json.")
        )
        binding = len(parts) == 3 and parts[:2] == ("channels", "feishu-bindings")
        application = path in application_files or any(
            path.startswith(name.rsplit("/", 1)[0] + "/." + name.rsplit("/", 1)[1] + ".")
            for name in application_files
        )
        if not (flat or fixed or temporary or binding or application):
            raise ProjectDeletionConflict("Deletion plan contains an unowned file")
    for rewrite in plan.rewrites:
        if rewrite.kind == "manifest" and rewrite.path == "_manifest.jsonl":
            continue
        if rewrite.kind == "diary" and re.fullmatch(
            r"agents/[A-Za-z0-9_-]+/diary/\d{4}-\d{2}-\d{2}\.md", rewrite.path
        ):
            continue
        if rewrite.kind == "pairing" and re.fullmatch(
            r"companion-devices/pair_[a-f0-9]{32}\.json", rewrite.path
        ):
            continue
        if rewrite.kind == "push" and re.fullmatch(
            r"companion-push/pair_[a-f0-9]{32}\.json", rewrite.path
        ):
            continue
        raise ProjectDeletionConflict("Deletion plan contains an unowned shared rewrite")


def verify_purge_sources(root: Path, plan: PurgePlan):
    """All surviving entries must still match; additions are never silently erased."""
    _validate_plan_paths(plan)
    current = DeletionCapture(root)
    for tree in plan.trees:
        current.tree(tree, "verify")
    expected = {entry.path: entry for entry in plan.entries}
    capture_companion_staging(current, plan.project_id, set(plan.companion_ids))
    # The durable gate should prohibit these writes. Verify membership anyway,
    # so a forgotten writer cannot leave new project records behind unnoticed.
    for directory, child in (
        ("runs", "run.json"),
        ("schedules", "schedule.json"),
        ("skill-proposals", "record.json"),
        ("intents", None),
        ("notifications", None),
        ("grants", None),
        ("feedback", None),
        ("channel-deliveries", None),
        ("shop-browsers", None),
        ("channels/feishu-bindings", None),
    ):
        for identifier, _value in current.records(
            directory,
            plan.project_id,
            child=child,
            mixed=directory == "shop-browsers",
            links={
                "run_id": set(plan.run_ids),
                "schedule_id": set(plan.schedule_ids),
                "intent_id": set(plan.intent_ids),
            }
            if child is None
            else None,
        ):
            path = f"{directory}/{identifier}" + (f"/{child}" if child else ".json")
            if path not in expected:
                raise ProjectDeletionConflict("New project records appeared after deletion review")
    if set(current.entries) - set(expected) or current.directories - set(plan.directories):
        raise ProjectDeletionConflict("New project files appeared after deletion review")
    for entry in plan.entries:
        current.entry(entry.path, "verify", optional=True)
    for path, entry in current.entries.items():
        original = expected[path]
        if (entry.kind, entry.sha256, entry.size_bytes) != (
            original.kind,
            original.sha256,
            original.size_bytes,
        ):
            raise ProjectDeletionConflict("Reviewed project files changed before erasure")


def purge_project_deletion(root: Path, plan: PurgePlan) -> dict:
    """Erase only a persisted reviewed plan; missing entries make retries safe."""
    try:
        return _purge_project_deletion(root, plan)
    except ProjectDeletionConflict:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        raise ProjectDeletionConflict(
            "Application erasure could not be completed; retry cleanup"
        ) from exc


def _purge_project_deletion(root: Path, plan: PurgePlan) -> dict:
    root = root.absolute()
    state_path = f"project-deletions/{plan.project_id}/state.json"
    state = parse_json(read_relative(root, state_path, 64 * 1024 * 1024))
    if not isinstance(state, dict) or not isinstance(state.get("plan"), dict):
        raise ProjectDeletionConflict("Project deletion intent is invalid")
    if (
        state.get("project_id") != plan.project_id
        or state.get("status") not in {"deleting", "blocked", "deleted"}
        or state.get("plan", {}).get("application") != plan.model_dump(mode="json")
    ):
        raise ProjectDeletionConflict("Project deletion does not match its durable intent")
    verify_purge_sources(root, plan)
    proof_path = f"project-deletions/{plan.project_id}/proofs.json"
    proof_bytes = _json_bytes(plan.proofs.model_dump(mode="json"))
    try:
        existing = read_relative(root, proof_path, JSON_LIMIT)
    except FileNotFoundError:
        _atomic_bytes(root, proof_path, proof_bytes)
    else:
        if parse_json(existing) != plan.proofs.model_dump(mode="json"):
            raise ProjectDeletionConflict("Project deletion provenance changed")
    for rewrite in sorted(
        plan.rewrites,
        key=lambda item: {"manifest": 0, "diary": 1, "pairing": 2, "push": 3}[item.kind],
    ):
        _rewrite(root, plan, rewrite)
    # Frozen Skill proposal packages intentionally have non-writable directories.
    # Only reviewed owned directories are made writable, through no-follow fds.
    for path in sorted(plan.directories, key=lambda value: (len(relative_parts(value)), value)):
        try:
            with directory_fd(root / path) as descriptor:
                mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
                os.fchmod(descriptor, mode | stat.S_IWUSR | stat.S_IXUSR)
                os.fsync(descriptor)
        except FileNotFoundError:
            continue
    removed = 0
    for entry in plan.entries:
        parts = relative_parts(entry.path)
        try:
            with ExitStack() as stack:
                if parts[0] in {"companion-devices", "companion-push"}:
                    stack.enter_context(_existing_store_lock(root, parts[0] + "/.transaction.lock"))
                root_fd = stack.enter_context(directory_fd(root))
                parent = stack.enter_context(child_directory(root_fd, parts[:-1]))
                # Recheck this exact leaf immediately before unlinking, never following links.
                check = DeletionCapture(root)
                check.entry(entry.path, entry.domain, optional=True)
                found = check.entries.get(entry.path)
                if found is None:
                    continue
                if found != entry:
                    raise ProjectDeletionConflict("Deletion source changed while erasing")
                os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
                removed += 1
        except FileNotFoundError:
            continue
    for path in sorted(plan.directories, key=lambda value: (-len(relative_parts(value)), value)):
        parts = relative_parts(path)
        try:
            with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
                os.rmdir(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            continue
    verify_purge_sources(root, plan)
    for entry in plan.entries:
        parts = relative_parts(entry.path)
        try:
            with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ProjectDeletionConflict("Project erasure left a reviewed source behind")
    return {"removed_files": removed, "retained": plan.retained}
