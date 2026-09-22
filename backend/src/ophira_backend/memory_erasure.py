"""File MemoryProvider's read-only contribution to an atomic conversation erasure."""

import hashlib
import json
import stat

from .backup_files import parse_json
from .constants import MemoryScope
from .erasure_files import encode
from .models import MemoryItem, MemoryTombstone


def jsonl(rows):
    return b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows)


def plan_run_erasure(provider, selection, runs, *, deleted_by, deleted_at, project=None):
    if provider.root != selection.root / "memory":
        raise ValueError("Memory erasure requires the configured transaction store.")
    sources, count = {run.id: run for run in runs}, 0
    for scope_name, scope_info in selection.children("memory"):
        scope = MemoryScope(scope_name)
        if not stat.S_ISDIR(scope_info.st_mode):
            raise ValueError("Invalid memory scope storage.")
        for scope_id, info in selection.children(f"memory/{scope_name}"):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Invalid memory scope storage.")
            directory = f"memory/{scope_name}/{scope_id}"
            if project is not None and scope == MemoryScope.PROJECT and scope_id == project.id:
                snapshot = provider.export_scope(scope, scope_id)
                for item in (
                    snapshot["items"]
                    + [row["item"] for row in snapshot["history"]]
                    + snapshot["tombstones"]
                ):
                    if (item["tenant_id"] != project.tenant_id
                            or item["identity_binding"] != project.identity_binding):
                        raise ValueError("Project memory identity mismatch.")
                count += len(snapshot["items"])
                selection.remove_tree(directory)
                selection.removed_directories.add(directory)
                continue
            if any(
                name not in {"items.json", "history.jsonl", "tombstones.jsonl"}
                for name, _ in selection.children(directory)
            ):
                raise ValueError("Memory storage has pending or unknown files. Recover it first.")
            # Capture all source bytes before port-level scope/history validation.
            raw = {
                name: selection.read(f"{directory}/{name}", optional=True, record=True)
                for name in ("items.json", "history.jsonl", "tombstones.jsonl")
            }
            provider.export_scope(scope, scope_id)
            items = [
                MemoryItem.model_validate(value) for value in parse_json(raw["items.json"] or b"[]")
            ]
            history = [
                parse_json(line)
                for line in (raw["history.jsonl"] or b"").splitlines()
                if line.strip()
            ]
            revisions = items + [MemoryItem.model_validate(row["item"]) for row in history]
            selected = {}
            for item in revisions:
                if item.source_run_id not in sources:
                    continue
                run = sources[item.source_run_id]
                if (
                    item.tenant_id != run.tenant_id
                    or item.identity_binding != run.identity_binding
                    or item.principal_id != run.principal_id
                ):
                    raise ValueError("Memory source identity does not match its conversation.")
                if item.id not in selected or item.version > selected[item.id].version:
                    selected[item.id] = item
            if not selected:
                continue
            # Deleting a source removes the whole memory lineage. Its audit marker
            # must describe the final version, including later human revisions.
            for item in revisions:
                if item.id in selected and item.version > selected[item.id].version:
                    selected[item.id] = item
            tombstones = [
                MemoryTombstone.model_validate_json(line)
                for line in (raw["tombstones.jsonl"] or b"").splitlines()
                if line.strip()
            ]
            deleted = {item.memory_id for item in tombstones}
            for item in selected.values():
                if item.id in deleted:
                    continue
                tombstones.append(
                    MemoryTombstone(
                        memory_id=item.id,
                        scope=scope,
                        scope_id=scope_id,
                        tenant_id=item.tenant_id,
                        principal_id=item.principal_id,
                        identity_binding=item.identity_binding,
                        last_version=item.version,
                        last_status=item.status,
                        source=item.source,
                        origin=item.origin,
                        sensitivity=item.sensitivity,
                        source_run_id=item.source_run_id,
                        content_sha256=hashlib.sha256(item.text.encode()).hexdigest(),
                        deleted_by=deleted_by,
                        deleted_at=deleted_at,
                        reason="conversation_deleted",
                    )
                )
            selection.change(
                f"{directory}/items.json",
                encode([item.model_dump(mode="json") for item in items if item.id not in selected]),
            )
            if raw["history.jsonl"] is not None:
                selection.change(
                    f"{directory}/history.jsonl",
                    jsonl([row for row in history if row["item"]["id"] not in selected]),
                )
            selection.change(
                f"{directory}/tombstones.jsonl",
                jsonl([item.model_dump(mode="json") for item in tombstones]),
            )
            if scope == MemoryScope.AGENT:
                if provider.agents_root != selection.root / "agents":
                    raise ValueError("Agent memory mirror is outside the transaction store.")
                path = f"agents/{scope_id}/MEMORY.md"
                mirror = selection.read(path, optional=True, record=True)
                if mirror is not None:
                    retained = b"".join(
                        line
                        for line in mirror.splitlines(keepends=True)
                        if not any(f"<!-- mem:{key} -->".encode() in line for key in selected)
                    )
                    selection.change(path, retained)
            count += len(selected)
    return count
