from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal, Protocol

from .constants import (
    MemoryKind,
    MemoryOrigin,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
    MemoryStatus,
)
from .models import MemoryItem, utc_now

MemoryDecisionValue = Literal["approve", "reject", "disable"]


class MemoryNotFoundError(KeyError):
    pass


class MemoryProvider(Protocol):
    """Governance port for agent memory. Cloud stages may adapt Managed Agents here."""

    def propose(
        self,
        *,
        scope: MemoryScope,
        scope_id: str,
        text: str,
        kind: MemoryKind,
        source: MemorySource,
        sensitivity: MemorySensitivity | None = None,
        confidence: float = 1.0,
        created_by: str = "system",
    ) -> MemoryItem: ...

    def decide(
        self, memory_id: str, decision: MemoryDecisionValue, reason: str = ""
    ) -> MemoryItem: ...

    def get(self, memory_id: str) -> MemoryItem: ...

    def list(
        self,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        status: MemoryStatus | None = None,
    ) -> list[MemoryItem]: ...

    def search(
        self,
        query: str | None = None,
        *,
        scopes: list[MemoryScope] | None = None,
        limit: int = 8,
    ) -> list[MemoryItem]: ...

    def rollback(self, memory_id: str, version: int) -> MemoryItem: ...

    def correct(
        self,
        memory_id: str,
        *,
        text: str,
        kind: MemoryKind | None = None,
        sensitivity: MemorySensitivity | None = None,
    ) -> MemoryItem: ...


def gather_approved(
    provider: MemoryProvider,
    pairs: list[tuple[MemoryScope, str]],
    query: str | None = None,
    limit: int = 8,
) -> list[MemoryItem]:
    """Merge approved memories across (scope, scope_id) pairs, newest first."""
    merged: dict[str, MemoryItem] = {}
    for scope, scope_id in pairs:
        for item in provider.list(scope=scope, scope_id=scope_id, status=MemoryStatus.APPROVED):
            merged[item.id] = item
    items = sorted(merged.values(), key=lambda item: item.updated_at, reverse=True)
    if query:
        needle = query.casefold()
        items = [item for item in items if needle in item.text.casefold()]
    return items[:limit]


class FileMemoryProvider:
    """File-backed MemoryProvider: per-scope item snapshots plus an append-only history."""

    def __init__(self, root: Path, agents_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.agents_root = agents_root.resolve() if agents_root else None
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _safe_id(value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not value or value in {".", ".."} or any(char not in allowed for char in value):
            raise ValueError("invalid memory identifier")
        return value

    def _scope_dir(self, scope: MemoryScope, scope_id: str) -> Path:
        return self.root / scope.value / self._safe_id(scope_id)

    def _items_path(self, scope: MemoryScope, scope_id: str) -> Path:
        return self._scope_dir(scope, scope_id) / "items.json"

    def _history_path(self, scope: MemoryScope, scope_id: str) -> Path:
        return self._scope_dir(scope, scope_id) / "history.jsonl"

    def _read_items(self, scope: MemoryScope, scope_id: str) -> list[MemoryItem]:
        path = self._items_path(scope, scope_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [MemoryItem.model_validate(item) for item in raw]

    def _write_items(self, scope: MemoryScope, scope_id: str, items: list[MemoryItem]) -> None:
        path = self._items_path(scope, scope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(f"{payload}\n", encoding="utf-8")
        os.replace(temporary, path)

    def _append_history(self, item: MemoryItem, action: str) -> None:
        path = self._history_path(item.scope, item.scope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": utc_now().isoformat(),
                "action": action,
                "item": item.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _sync_memory_md(self, item: MemoryItem) -> None:
        """Mirror agent-scope memories into the agent's MEMORY.md via marker lines.

        Approved items get an upserted marked line; any other status removes it.
        Lines without this item's marker (including user freehand edits) are kept.
        """
        if self.agents_root is None or item.scope != MemoryScope.AGENT:
            return
        core_dir = self.agents_root / self._safe_id(item.scope_id)
        if not core_dir.exists():
            return
        path = core_dir / "MEMORY.md"
        marker = f"<!-- mem:{item.id} -->"
        lines = (
            path.read_text(encoding="utf-8").splitlines()
            if path.exists()
            else ["# 长期记忆", ""]
        )
        lines = [line for line in lines if marker not in line]
        if item.status == MemoryStatus.APPROVED:
            text = " ".join(item.text.split())
            lines.append(
                f"- [{item.kind.value}][置信度 {item.confidence:.2f}] {text} {marker}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def propose(
        self,
        *,
        scope: MemoryScope,
        scope_id: str,
        text: str,
        kind: MemoryKind,
        source: MemorySource,
        sensitivity: MemorySensitivity | None = None,
        confidence: float = 1.0,
        created_by: str = "system",
        origin: MemoryOrigin | None = None,
        source_run_id: str | None = None,
        source_anchor: str = "",
    ) -> MemoryItem:
        item = MemoryItem(
            scope=scope,
            scope_id=scope_id,
            text=text,
            kind=kind,
            source=source,
            origin=origin
            or (
                MemoryOrigin.OWNER
                if source == MemorySource.USER
                else MemoryOrigin.AGENT
            ),
            source_run_id=source_run_id,
            source_anchor=source_anchor,
            confidence=confidence,
            sensitivity=sensitivity or MemorySensitivity.NORMAL,
            created_by=created_by,
        )
        # User-entered memory is already confirmed; agent proposals always need approval.
        # Sensitive agent proposals are never auto-approved by any path.
        if source == MemorySource.USER:
            item.status = MemoryStatus.APPROVED
        with self._lock_for(f"{item.scope}:{item.scope_id}"):
            items = self._read_items(item.scope, item.scope_id)
            items.append(item)
            self._write_items(item.scope, item.scope_id, items)
            self._append_history(item, "propose")
            self._sync_memory_md(item)
        return item

    def get(self, memory_id: str) -> MemoryItem:
        self._safe_id(memory_id)
        for scope in MemoryScope:
            scope_root = self.root / scope.value
            if not scope_root.exists():
                continue
            for items_path in scope_root.glob("*/items.json"):
                for item in self._read_items(scope, items_path.parent.name):
                    if item.id == memory_id:
                        return item
        raise MemoryNotFoundError(memory_id)

    def _update(self, memory_id: str, action: str, mutate) -> MemoryItem:
        current = self.get(memory_id)
        with self._lock_for(f"{current.scope}:{current.scope_id}"):
            items = self._read_items(current.scope, current.scope_id)
            for index, item in enumerate(items):
                if item.id == memory_id:
                    updated = mutate(item)
                    updated.version = item.version + 1
                    updated.updated_at = utc_now()
                    items[index] = updated
                    self._write_items(current.scope, current.scope_id, items)
                    self._append_history(updated, action)
                    self._sync_memory_md(updated)
                    return updated
        raise MemoryNotFoundError(memory_id)

    def decide(
        self, memory_id: str, decision: MemoryDecisionValue, reason: str = ""
    ) -> MemoryItem:
        status_by_decision = {
            "approve": MemoryStatus.APPROVED,
            "reject": MemoryStatus.REJECTED,
            "disable": MemoryStatus.DISABLED,
        }
        target = status_by_decision[decision]
        if target == MemoryStatus.APPROVED:
            from .constants import APPROVED_MEMORY_CAP, APPROVED_MEMORY_CHARS

            current = self.get(memory_id)
            existing = self.list(scope=current.scope, scope_id=current.scope_id)
            approved = [
                item
                for item in existing
                if item.status == MemoryStatus.APPROVED and item.id != current.id
            ]
            chars = sum(len(item.text) for item in approved)
            over_count = len(approved) >= APPROVED_MEMORY_CAP
            over_chars = chars + len(current.text) > APPROVED_MEMORY_CHARS
            if over_count or over_chars:
                raise ValueError(
                    f"稳定记忆已达上限（{APPROVED_MEMORY_CAP} 条 / {APPROVED_MEMORY_CHARS} 字），"
                    "请先合并或停用旧条目。"
                )

        def mutate(item: MemoryItem) -> MemoryItem:
            return item.model_copy(update={"status": target})

        action = f"decide.{decision}:{reason}" if reason else f"decide.{decision}"
        return self._update(memory_id, action, mutate)

    def correct(
        self,
        memory_id: str,
        *,
        text: str,
        kind: MemoryKind | None = None,
        sensitivity: MemorySensitivity | None = None,
    ) -> MemoryItem:
        """Apply an explicit owner correction as a new approved version."""
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("memory correction cannot be empty")
        current = self.get(memory_id)
        existing = self.list(scope=current.scope, scope_id=current.scope_id)
        approved = [
            item
            for item in existing
            if item.status == MemoryStatus.APPROVED and item.id != current.id
        ]
        from .constants import APPROVED_MEMORY_CAP, APPROVED_MEMORY_CHARS

        if len(approved) >= APPROVED_MEMORY_CAP or (
            sum(len(item.text) for item in approved) + len(normalized)
            > APPROVED_MEMORY_CHARS
        ):
            raise ValueError(
                f"稳定记忆已达上限（{APPROVED_MEMORY_CAP} 条 / {APPROVED_MEMORY_CHARS} 字），"
                "请先合并或停用旧条目。"
            )

        def mutate(item: MemoryItem) -> MemoryItem:
            return item.model_copy(
                update={
                    "text": normalized,
                    "kind": kind or item.kind,
                    "sensitivity": sensitivity or item.sensitivity,
                    "source": MemorySource.USER,
                    "origin": MemoryOrigin.OWNER,
                    "confidence": 1.0,
                    "status": MemoryStatus.APPROVED,
                }
            )

        return self._update(memory_id, "correct.owner", mutate)

    def list(
        self,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        status: MemoryStatus | None = None,
    ) -> list[MemoryItem]:
        scopes = [scope] if scope else list(MemoryScope)
        result: list[MemoryItem] = []
        for item_scope in scopes:
            scope_root = self.root / item_scope.value
            if not scope_root.exists():
                continue
            if scope_id:
                candidates = [scope_root / self._safe_id(scope_id)]
            else:
                candidates = [path for path in scope_root.iterdir() if path.is_dir()]
            for candidate in candidates:
                for item in self._read_items(item_scope, candidate.name):
                    if status is None or item.status == status:
                        result.append(item)
        return sorted(result, key=lambda item: item.updated_at, reverse=True)

    def search(
        self,
        query: str | None = None,
        *,
        scopes: list[MemoryScope] | None = None,
        limit: int = 8,
    ) -> list[MemoryItem]:
        candidates = [
            item
            for item in self.list(status=MemoryStatus.APPROVED)
            if scopes is None or item.scope in scopes
        ]
        if query:
            needle = query.casefold()
            candidates = [item for item in candidates if needle in item.text.casefold()]
        return candidates[:limit]

    def rollback(self, memory_id: str, version: int) -> MemoryItem:
        current = self.get(memory_id)
        history_path = self._history_path(current.scope, current.scope_id)
        snapshot: MemoryItem | None = None
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                item = MemoryItem.model_validate(entry["item"])
                if item.id == memory_id and item.version == version:
                    snapshot = item
        if snapshot is None:
            raise MemoryNotFoundError(f"{memory_id}@v{version}")

        def mutate(_item: MemoryItem) -> MemoryItem:
            return snapshot

        return self._update(memory_id, f"rollback.v{version}", mutate)
