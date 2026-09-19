from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime
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
from .models import (
    MemoryItem,
    MemoryPublicItem,
    MemoryRetrievalHit,
    MemoryRetrievalMetadata,
    MemoryRetrievalResponse,
    MemoryTombstone,
    PrincipalContext,
    RunRecord,
    utc_now,
)
from .project_lifecycle import ProjectLifecycle

MemoryDecisionValue = Literal["approve", "reject", "disable"]

_SCOPE_LOCKS: dict[tuple[Path, str], threading.RLock] = {}
_SCOPE_LOCKS_GUARD = threading.Lock()
_HELD_SCOPE_LOCKS = threading.local()


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
        origin: MemoryOrigin | None = None,
        source_run_id: str | None = None,
        source_anchor: str = "",
        principal: PrincipalContext | RunRecord | None = None,
        expires_at: datetime | None = None,
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

    def list_tombstones(
        self,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
    ) -> list[MemoryTombstone]: ...

    def search(
        self,
        query: str | None = None,
        *,
        scopes: list[MemoryScope] | None = None,
        limit: int = 8,
        at: datetime | None = None,
    ) -> list[MemoryItem]: ...

    def retrieve(
        self,
        query: str,
        *,
        pairs: list[tuple[MemoryScope, str]],
        identity: PrincipalContext | RunRecord | None,
        limit: int = 8,
        at: datetime | None = None,
    ) -> MemoryRetrievalResponse: ...

    def rollback(self, memory_id: str, version: int) -> MemoryItem: ...

    def set_expiry(self, memory_id: str, expires_at: datetime | None) -> MemoryItem: ...

    def delete(self, memory_id: str, *, deleted_by: str, reason: str) -> MemoryTombstone: ...

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
    identity: PrincipalContext | RunRecord | None = None,
    at: datetime | None = None,
) -> list[MemoryItem]:
    """Merge approved memories across (scope, scope_id) pairs, newest first."""
    merged: dict[str, MemoryItem] = {}
    for scope, scope_id in pairs:
        for item in provider.list(scope=scope, scope_id=scope_id, status=MemoryStatus.APPROVED):
            merged[item.id] = item
    evaluated_at = at or utc_now()
    items = [
        item
        for item in merged.values()
        if memory_visible_to(item, identity)
        and not memory_is_expired(item, at=evaluated_at)
    ]
    items.sort(key=lambda item: item.updated_at, reverse=True)
    if query:
        needle = query.casefold()
        items = [item for item in items if needle in item.text.casefold()]
    return items[:limit]


def memory_is_expired(item: MemoryItem, *, at: datetime | None = None) -> bool:
    evaluated_at = at or utc_now()
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("memory expiry comparison must be timezone-aware")
    return item.expires_at is not None and item.expires_at <= evaluated_at


def public_memory_item(
    item: MemoryItem,
    *,
    at: datetime | None = None,
) -> MemoryPublicItem:
    return MemoryPublicItem(
        id=item.id,
        scope=item.scope,
        scope_id=item.scope_id,
        identity_binding=item.identity_binding,
        tenant_id=item.tenant_id,
        principal_id=item.principal_id,
        text=item.text,
        kind=item.kind,
        source=item.source,
        origin=item.origin,
        source_run_id=item.source_run_id,
        source_anchor=item.source_anchor,
        confidence=item.confidence,
        sensitivity=item.sensitivity,
        status=item.status,
        version=item.version,
        created_by=item.created_by,
        created_at=item.created_at,
        updated_at=item.updated_at,
        expires_at=item.expires_at,
        expired=memory_is_expired(item, at=at),
    )


def _normalize_retrieval_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def retrieve_approved(
    provider: MemoryProvider,
    pairs: list[tuple[MemoryScope, str]],
    query: str,
    *,
    identity: PrincipalContext | RunRecord | None,
    limit: int = 8,
    at: datetime | None = None,
) -> MemoryRetrievalResponse:
    """Deterministically prefetch request-relevant, approved, visible memory."""

    evaluated_at = at or utc_now()
    normalized_query = _normalize_retrieval_text(query)
    if not normalized_query:
        raise ValueError("memory retrieval query cannot be empty")
    terms = list(dict.fromkeys(normalized_query.split(" ")))
    distinct_pairs = list(dict.fromkeys(pairs))
    merged: dict[str, MemoryItem] = {}
    for scope, scope_id in distinct_pairs:
        for item in provider.list(scope=scope, scope_id=scope_id, status=MemoryStatus.APPROVED):
            if memory_visible_to(item, identity):
                merged[item.id] = item

    candidates = list(merged.values())
    eligible = [
        item for item in candidates if not memory_is_expired(item, at=evaluated_at)
    ]
    matched: list[tuple[MemoryItem, bool, list[str], int]] = []
    for item in eligible:
        normalized_text = _normalize_retrieval_text(item.text)
        matched_terms = [term for term in terms if term in normalized_text]
        if len(matched_terms) != len(terms):
            continue
        occurrences = sum(normalized_text.count(term) for term in terms)
        matched.append(
            (item, normalized_query in normalized_text, matched_terms, occurrences)
        )
    matched.sort(
        key=lambda row: (
            not row[1],
            -row[3],
            -row[0].updated_at.timestamp(),
            row[0].id,
        )
    )
    hits = [
        MemoryRetrievalHit(
            rank=index,
            exact_query_match=exact_match,
            matched_terms=matched_terms,
            term_occurrences=occurrences,
            item=public_memory_item(item, at=evaluated_at),
        )
        for index, (item, exact_match, matched_terms, occurrences) in enumerate(
            matched[:limit],
            start=1,
        )
    ]
    return MemoryRetrievalResponse(
        metadata=MemoryRetrievalMetadata(
            query=query,
            normalized_query=normalized_query,
            terms=terms,
            requested_scope_pairs=[
                f"{scope.value}:{scope_id}" for scope, scope_id in distinct_pairs
            ],
            evaluated_at=evaluated_at,
            authorized_approved_candidates=len(candidates),
            excluded_expired=len(candidates) - len(eligible),
            matched=len(matched),
            returned=len(hits),
        ),
        hits=hits,
    )


def memory_visible_to(
    item: MemoryItem,
    identity: PrincipalContext | RunRecord | None,
) -> bool:
    """Enforce identity scope without treating memory metadata as authority."""

    if identity is None:
        return item.identity_binding == "local_only"
    if isinstance(identity, RunRecord) and identity.identity_binding == "local_only":
        return item.identity_binding == "local_only"
    if item.identity_binding != "authenticated_principal":
        return False
    if item.tenant_id != identity.tenant_id:
        return False
    if item.scope == MemoryScope.PROJECT:
        return True
    return item.principal_id == identity.principal_id


class FileMemoryProvider:
    """File-backed snapshots, version history, and durable deletion tombstones."""

    def __init__(self, root: Path, agents_root: Path | None = None) -> None:
        self.root = root.resolve()
        self._ensure_directory(self.root)
        self.agents_root = agents_root.resolve() if agents_root else None
        self._locks_dir = self.root / ".memory-locks"
        self._locks_dir.mkdir(mode=0o700, exist_ok=True)
        if self._locks_dir.is_symlink():
            raise ValueError("memory lock directory must not be a symlink")
        self._recover_hard_deletions()
        self._reconcile_expired_agent_markers()

    @contextmanager
    def _lock_for(self, key: str) -> Iterator[None]:
        """Short scope transaction shared by providers, threads, and API workers."""
        identity = (self.root, key)
        with _SCOPE_LOCKS_GUARD:
            lock = _SCOPE_LOCKS.setdefault(identity, threading.RLock())
        scope, _, scope_id = key.partition(":")
        lifecycle = (ProjectLifecycle(self.root.parent).lease(scope_id)
                     if scope == MemoryScope.PROJECT.value else nullcontext())
        with lifecycle, lock:
            held = getattr(_HELD_SCOPE_LOCKS, "keys", set())
            if identity in held:
                yield
                return
            path = self._locks_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
            descriptor = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("memory lock must be a private regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _HELD_SCOPE_LOCKS.keys = held | {identity}
                yield
            finally:
                _HELD_SCOPE_LOCKS.keys = held
                os.close(descriptor)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _ensure_directory(cls, path: Path) -> None:
        if path.is_dir():
            return
        cls._ensure_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        cls._sync_directory(path.parent)

    @classmethod
    def _atomic_text(cls, path: Path, content: str) -> None:
        cls._ensure_directory(path.parent)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".ophira-memory-{path.name}-", dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def _discard_staging(cls, directory: Path, names: tuple[str, ...]) -> None:
        """Remove abandoned private writes only while owning the scope transaction."""
        removed = False
        for name in names:
            candidates = list(directory.glob(f".ophira-memory-{name}-*"))
            if name in {"items.json", "history.jsonl"}:
                candidates.append(directory / f"{name}.tmp")
            for path in candidates:
                if not path.exists() and not path.is_symlink():
                    continue
                path.unlink()
                removed = True
        if removed:
            cls._sync_directory(directory)

    def _discard_scope_staging(self, scope: MemoryScope, scope_id: str) -> None:
        self._discard_staging(self._scope_dir(scope, scope_id), ("items.json", "history.jsonl"))
        if scope == MemoryScope.AGENT and self.agents_root is not None:
            self._discard_staging(self.agents_root / self._safe_id(scope_id), ("MEMORY.md",))

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

    def _tombstones_path(self, scope: MemoryScope, scope_id: str) -> Path:
        return self._scope_dir(scope, scope_id) / "tombstones.jsonl"

    def _read_items_raw(self, scope: MemoryScope, scope_id: str) -> list[MemoryItem]:
        path = self._items_path(scope, scope_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [MemoryItem.model_validate(item) for item in raw]

    def _read_tombstones(
        self,
        scope: MemoryScope,
        scope_id: str,
    ) -> list[MemoryTombstone]:
        path = self._tombstones_path(scope, scope_id)
        if not path.exists():
            return []
        return [
            MemoryTombstone.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _read_items(self, scope: MemoryScope, scope_id: str) -> list[MemoryItem]:
        with self._lock_for(f"{scope}:{scope_id}"):
            deleted_ids = {
                tombstone.memory_id for tombstone in self._read_tombstones(scope, scope_id)
            }
            return [
                item
                for item in self._read_items_raw(scope, scope_id)
                if item.id not in deleted_ids
            ]

    def _write_items(self, scope: MemoryScope, scope_id: str, items: list[MemoryItem]) -> None:
        path = self._items_path(scope, scope_id)
        payload = json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._atomic_text(path, f"{payload}\n")

    def _append_history(self, item: MemoryItem, action: str) -> None:
        path = self._history_path(item.scope, item.scope_id)
        self._ensure_directory(path.parent)
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
        self._sync_directory(path.parent)

    def _append_tombstone(self, tombstone: MemoryTombstone) -> None:
        path = self._tombstones_path(tombstone.scope, tombstone.scope_id)
        self._ensure_directory(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(tombstone.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._sync_directory(path.parent)

    def _scrub_history(self, scope: MemoryScope, scope_id: str, memory_id: str) -> None:
        path = self._history_path(scope, scope_id)
        if not path.exists():
            return
        retained: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            item = entry.get("item")
            if isinstance(item, dict) and item.get("id") == memory_id:
                continue
            retained.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        self._atomic_text(path, "\n".join(retained) + "\n" if retained else "")

    def _remove_memory_md_marker(self, scope_id: str, memory_id: str) -> None:
        if self.agents_root is None:
            return
        core_dir = self.agents_root / self._safe_id(scope_id)
        path = core_dir / "MEMORY.md"
        if not path.exists():
            return
        marker = f"<!-- mem:{memory_id} -->"
        lines = path.read_text(encoding="utf-8").splitlines()
        retained = [line for line in lines if marker not in line]
        if retained != lines:
            self._atomic_text(path, "\n".join(retained) + "\n")

    def _recover_hard_deletions(self) -> None:
        """Finish content scrubbing after a crash once a tombstone is durable."""

        for scope in MemoryScope:
            scope_root = self.root / scope.value
            if not scope_root.exists():
                continue
            for scope_dir in (path for path in scope_root.iterdir() if path.is_dir()):
                if (scope == MemoryScope.PROJECT
                        and ProjectLifecycle(self.root.parent).has_deletion(scope_dir.name)):
                    continue
                with self._lock_for(f"{scope}:{scope_dir.name}"):
                    self._discard_scope_staging(scope, scope_dir.name)
                    tombstones = self._read_tombstones(scope, scope_dir.name)
                    if not tombstones:
                        continue
                    deleted_ids = {item.memory_id for item in tombstones}
                    raw_items = self._read_items_raw(scope, scope_dir.name)
                    retained = [item for item in raw_items if item.id not in deleted_ids]
                    if len(retained) != len(raw_items):
                        self._write_items(scope, scope_dir.name, retained)
                    for tombstone in tombstones:
                        self._scrub_history(scope, scope_dir.name, tombstone.memory_id)
                        if scope == MemoryScope.AGENT:
                            self._remove_memory_md_marker(scope_dir.name, tombstone.memory_id)

    def _reconcile_expired_agent_markers(self) -> None:
        if self.agents_root is None:
            return
        scope_root = self.root / MemoryScope.AGENT.value
        if not scope_root.exists():
            return
        for scope_dir in (path for path in scope_root.iterdir() if path.is_dir()):
            with self._lock_for(f"{MemoryScope.AGENT}:{scope_dir.name}"):
                for item in self._read_items(MemoryScope.AGENT, scope_dir.name):
                    if memory_is_expired(item):
                        self._remove_memory_md_marker(scope_dir.name, item.id)

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
        if item.status == MemoryStatus.APPROVED and not memory_is_expired(item):
            text = " ".join(item.text.split())
            lines.append(
                f"- [{item.kind.value}][置信度 {item.confidence:.2f}] {text} {marker}"
            )
        self._atomic_text(path, "\n".join(lines) + "\n")

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
        principal: PrincipalContext | RunRecord | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryItem:
        authenticated_principal = principal is not None and (
            not isinstance(principal, RunRecord)
            or principal.identity_binding == "authenticated_principal"
        )
        identity = (
            {
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "principal_role": principal.role,
                "principal_session_id": principal.session_id,
                "identity_binding": "authenticated_principal",
            }
            if authenticated_principal
            else {}
        )
        created_at = utc_now()
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("memory expiry must be timezone-aware")
            if expires_at <= created_at:
                raise ValueError("memory expiry must be in the future")
        item = MemoryItem(
            scope=scope,
            scope_id=scope_id,
            **identity,
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
            created_by=(
                principal.principal_id
                if authenticated_principal and principal is not None
                else created_by
            ),
            created_at=created_at,
            updated_at=created_at,
            expires_at=expires_at,
        )
        # User-entered memory is already confirmed; agent proposals always need approval.
        # Sensitive agent proposals are never auto-approved by any path.
        if source == MemorySource.USER:
            item.status = MemoryStatus.APPROVED
        with self._lock_for(f"{item.scope}:{item.scope_id}"):
            items = self._read_items(item.scope, item.scope_id)
            self._validate_capacity(item, items)
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
                if (scope == MemoryScope.PROJECT
                        and ProjectLifecycle(self.root.parent).has_deletion(
                            items_path.parent.name,
                        )):
                    continue
                for item in self._read_items(scope, items_path.parent.name):
                    if item.id == memory_id:
                        return item
        raise MemoryNotFoundError(memory_id)

    @staticmethod
    def _validate_capacity(candidate: MemoryItem, items: list[MemoryItem]) -> None:
        if candidate.status != MemoryStatus.APPROVED or memory_is_expired(candidate):
            return
        from .constants import APPROVED_MEMORY_CAP, APPROVED_MEMORY_CHARS

        approved = [item for item in items if item.id != candidate.id
                    and item.status == MemoryStatus.APPROVED and not memory_is_expired(item)]
        if len(approved) >= APPROVED_MEMORY_CAP or (
            sum(len(item.text) for item in approved) + len(candidate.text) > APPROVED_MEMORY_CHARS
        ):
            raise ValueError(
                f"稳定记忆已达上限（{APPROVED_MEMORY_CAP} 条 / {APPROVED_MEMORY_CHARS} 字），"
                "请先合并或停用旧条目。"
            )

    def _update(self, memory_id: str, action: str, mutate) -> MemoryItem:
        current = self.get(memory_id)
        with self._lock_for(f"{current.scope}:{current.scope_id}"):
            items = self._read_items(current.scope, current.scope_id)
            for index, item in enumerate(items):
                if item.id == memory_id:
                    updated = mutate(item)
                    self._validate_capacity(updated, items)
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

        def mutate(item: MemoryItem) -> MemoryItem:
            if target == MemoryStatus.APPROVED and memory_is_expired(item):
                raise ValueError("expired memory requires a future expiry before approval")
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
        def mutate(item: MemoryItem) -> MemoryItem:
            if memory_is_expired(item):
                raise ValueError("expired memory requires a future expiry before correction")
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

    def set_expiry(self, memory_id: str, expires_at: datetime | None) -> MemoryItem:
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("memory expiry must be timezone-aware")
            if expires_at <= utc_now():
                raise ValueError("memory expiry must be in the future")

        def mutate(item: MemoryItem) -> MemoryItem:
            if expires_at is not None and expires_at <= utc_now():
                raise ValueError("memory expiry must be in the future")
            return item.model_copy(update={"expires_at": expires_at})

        action = "expiry.clear" if expires_at is None else "expiry.set"
        return self._update(memory_id, action, mutate)

    def delete(
        self,
        memory_id: str,
        *,
        deleted_by: str,
        reason: str,
    ) -> MemoryTombstone:
        """Hard-delete content after first durably recording a content-free tombstone."""

        self._safe_id(memory_id)
        normalized_reason = " ".join(reason.split())
        if not normalized_reason:
            raise ValueError("memory deletion reason cannot be empty")
        current = self.get(memory_id)
        lock_key = f"{current.scope}:{current.scope_id}"
        with self._lock_for(lock_key):
            # Discovery only identifies the scope. Audit the latest version after
            # acquiring its transaction, including any writer we just waited for.
            current = next((item for item in self._read_items(current.scope, current.scope_id)
                            if item.id == memory_id), None)
            if current is None:
                raise MemoryNotFoundError(memory_id)
            tombstone = MemoryTombstone(
                memory_id=current.id,
                scope=current.scope,
                scope_id=current.scope_id,
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                identity_binding=current.identity_binding,
                last_version=current.version,
                last_status=current.status,
                source=current.source,
                origin=current.origin,
                sensitivity=current.sensitivity,
                source_run_id=current.source_run_id,
                content_sha256=hashlib.sha256(current.text.encode("utf-8")).hexdigest(),
                deleted_by=deleted_by,
                reason=normalized_reason,
            )
            # The tombstone is authoritative and fsynced first. If the process exits
            # during content scrubbing, startup recovery finishes the deletion.
            self._append_tombstone(tombstone)
            self._discard_scope_staging(current.scope, current.scope_id)
            items = [
                item
                for item in self._read_items_raw(current.scope, current.scope_id)
                if item.id != memory_id
            ]
            self._write_items(current.scope, current.scope_id, items)
            self._scrub_history(current.scope, current.scope_id, memory_id)
            if current.scope == MemoryScope.AGENT:
                self._remove_memory_md_marker(current.scope_id, current.id)
            return tombstone

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
                with self._lock_for(f"{item_scope}:{candidate.name}"):
                    for item in self._read_items(item_scope, candidate.name):
                        if memory_is_expired(item):
                            self._sync_memory_md(item)
                        if status is None or item.status == status:
                            result.append(item)
        return sorted(result, key=lambda item: item.updated_at, reverse=True)

    def list_tombstones(
        self,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
    ) -> list[MemoryTombstone]:
        scopes = [scope] if scope else list(MemoryScope)
        result: list[MemoryTombstone] = []
        for item_scope in scopes:
            scope_root = self.root / item_scope.value
            if not scope_root.exists():
                continue
            candidates = (
                [scope_root / self._safe_id(scope_id)]
                if scope_id
                else [path for path in scope_root.iterdir() if path.is_dir()]
            )
            for candidate in candidates:
                with self._lock_for(f"{item_scope}:{candidate.name}"):
                    result.extend(self._read_tombstones(item_scope, candidate.name))
        return sorted(result, key=lambda item: item.deleted_at, reverse=True)

    def search(
        self,
        query: str | None = None,
        *,
        scopes: list[MemoryScope] | None = None,
        limit: int = 8,
        at: datetime | None = None,
    ) -> list[MemoryItem]:
        evaluated_at = at or utc_now()
        candidates = [
            item
            for item in self.list(status=MemoryStatus.APPROVED)
            if scopes is None or item.scope in scopes
        ]
        candidates = [
            item for item in candidates if not memory_is_expired(item, at=evaluated_at)
        ]
        if query:
            needle = query.casefold()
            candidates = [item for item in candidates if needle in item.text.casefold()]
        return candidates[:limit]

    def retrieve(
        self,
        query: str,
        *,
        pairs: list[tuple[MemoryScope, str]],
        identity: PrincipalContext | RunRecord | None,
        limit: int = 8,
        at: datetime | None = None,
    ) -> MemoryRetrievalResponse:
        return retrieve_approved(
            self,
            pairs,
            query,
            identity=identity,
            limit=limit,
            at=at,
        )

    def rollback(self, memory_id: str, version: int) -> MemoryItem:
        def mutate(current: MemoryItem) -> MemoryItem:
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
            return snapshot

        return self._update(memory_id, f"rollback.v{version}", mutate)
