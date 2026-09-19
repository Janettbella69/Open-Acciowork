"""Reviewed application copies of one conversation; project outputs stay independent."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .constants import TERMINAL_RUN_STATUSES
from .project_deletion_capture import (
    DeletionCapture,
    ProjectDeletionConflict,
    PurgeEntry,
    PurgeRewrite,
    _capture_shared,
)
from .project_deletion_purge import _rewrite
from .skill_proposal_files import child_directory, directory_fd, relative_parts

RETAINED = [
    "The project, other conversations, uploads, artifacts, evidence and source receipts "
    "are retained.",
    "Project recovery snapshots, memories, schedules, task experience, Skill proposals "
    "and commercial records are retained.",
    "Copies in retained project records, external messages, exports and operator backups "
    "require separate management.",
    "Content-free deletion records, Run provenance and stable coordination locks are retained.",
]
LINKED_STORES = ("notifications", "feedback", "channel-deliveries")


class ConversationPurgePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    project_revision: int
    run_ids: list[str]
    trees: list[str]
    entries: list[PurgeEntry]
    directories: list[str]
    rewrites: list[PurgeRewrite]
    blockers: list[str]

    @property
    def shop_ids(self):
        return []


def _linked(capture, project_id, run_ids):
    selected = {}
    for store in LINKED_STORES:
        for name in capture.names(store):
            if name.endswith(".lock"):
                continue
            value = capture.json(f"{store}/{name}")
            if not isinstance(value, dict):
                raise ProjectDeletionConflict("Invalid conversation-linked record")
            if value.get("run_id") not in run_ids:
                continue
            if value.get("project_id") not in {None, project_id}:
                raise ProjectDeletionConflict("Conversation-linked record has foreign scope")
            capture.entry(f"{store}/{name}", store)
            if store == "notifications":
                selected[value["id"]] = value
    return selected


def capture_conversation(root, project, runs):
    capture = DeletionCapture(root)
    by_id = {run.id: run for run in runs}
    blockers = []
    for run in runs:
        if run.project_id != project.id:
            raise ProjectDeletionConflict("Conversation Run has foreign scope")
        if run.status not in TERMINAL_RUN_STATUSES:
            blockers.append("Finish or cancel every Run in this conversation before deleting it.")
        capture.tree(f"runs/{run.id}", "conversations")
        if (
            run.schedule_id
            and capture.json(
                f"task-experiences/{run.schedule_id}.transaction.json",
                optional=True,
            )
            is not None
        ):
            blockers.append(
                "Resolve pending task experience updates before deleting this conversation."
            )
    notifications = _linked(capture, project.id, set(by_id))
    rewrites, _pair_ids = _capture_shared(capture, project, by_id, notifications, conversation=True)
    return ConversationPurgePlan(
        project_id=project.id,
        project_revision=project.revision,
        run_ids=sorted(by_id),
        trees=sorted(capture.trees),
        entries=sorted(capture.entries.values(), key=lambda row: row.path),
        directories=sorted(capture.directories),
        rewrites=rewrites,
        blockers=sorted(set(blockers)),
    )


def verify_conversation_sources(root: Path, plan: ConversationPurgePlan):
    expected = {entry.path: entry for entry in plan.entries}
    trees = {f"runs/{identifier}" for identifier in plan.run_ids}
    if len(expected) != len(plan.entries) or set(plan.trees) != trees:
        raise ProjectDeletionConflict("Invalid conversation erasure scope")
    for path in [*plan.directories, *(entry.path for entry in plan.entries)]:
        parts = relative_parts(path)
        if any(path == tree or path.startswith(tree + "/") for tree in trees):
            continue
        if len(parts) != 2 or parts[0] not in LINKED_STORES or path in plan.directories:
            raise ProjectDeletionConflict("Invalid conversation erasure scope")
    current = DeletionCapture(root)
    for tree in plan.trees:
        current.tree(tree, "verify")
    _linked(current, plan.project_id, set(plan.run_ids))
    for entry in plan.entries:
        current.entry(entry.path, "verify", optional=True)
    if current.directories - set(plan.directories) or set(current.entries) - set(expected):
        raise ProjectDeletionConflict("Conversation changed after deletion review")
    for path, item in current.entries.items():
        if item.model_copy(update={"domain": expected[path].domain}) != expected[path]:
            raise ProjectDeletionConflict("Conversation changed after deletion review")


def purge_conversation(root: Path, plan: ConversationPurgePlan):
    """Caller has checked the persisted intent and owns the exclusive project lease."""
    verify_conversation_sources(root, plan)
    for rewrite in plan.rewrites:
        if rewrite.kind not in {"run-manifest", "diary", "push"}:
            raise ProjectDeletionConflict("Invalid conversation shared rewrite")
        _rewrite(root, plan, rewrite)
    for entry in plan.entries:
        check = DeletionCapture(root)
        check.entry(entry.path, entry.domain, optional=True)
        if not check.entries:
            continue
        if check.entries[entry.path] != entry:
            raise ProjectDeletionConflict("Conversation changed during erasure")
        parts = relative_parts(entry.path)
        try:
            with directory_fd(root) as fd, child_directory(fd, parts[:-1]) as parent:
                os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            pass
    for path in sorted(plan.directories, key=lambda value: (-value.count("/"), value)):
        parts = relative_parts(path)
        try:
            with directory_fd(root) as fd, child_directory(fd, parts[:-1]) as parent:
                os.rmdir(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            pass
    verify_conversation_sources(root, plan)
    cap = DeletionCapture(root)
    for entry in plan.entries:
        cap.entry(entry.path, entry.domain, optional=True)
    if cap.entries:
        raise ProjectDeletionConflict("Conversation erasure is incomplete")
