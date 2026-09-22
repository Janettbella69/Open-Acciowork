from __future__ import annotations

import hashlib
import json
import os
import threading
import unicodedata
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
        source_run_id: str | None = None,
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

    def export_scope(
        self, scope: MemoryScope, scope_id: str, *, max_bytes: int = 16 * 1024 * 1024,
    ) -> dict: ...

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
        self.root.mkdir(parents=True, exist_ok=True)
        self.agents_root = agents_root.resolve() if agents_root else None
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._recover_hard_deletions()
        self._reconcile_expired_agent_markers()

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

    def _tombstones_path(self, scope: MemoryScope, scope_id: str) -> Path:
        return self._scope_dir(scope, scope_id) / "tombstones.jsonl"

    def _read_items_raw(self, scope: MemoryScope, scope_id: str) -> list[MemoryItem]:
        path = self._items_path(scope, scope_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [MemoryItem.model_validate(item) for item in raw]

    def export_scope(
        self, scope: MemoryScope, scope_id: str, *, max_bytes: int = 16 * 1024 * 1024,
    ) -> dict:
        """Read an exact scope with history, respecting authoritative deletions.

        Unlike interactive listing, archive capture must bound all source bytes,
        reject links and detect concurrent writers without changing memory state.
        """
        from .backup_files import parse_json
        from .skill_proposal_files import read_relative

        root = self._scope_dir(scope, scope_id)
        names = ("items.json", "history.jsonl", "tombstones.jsonl")

        def capture():
            remaining, result = max_bytes, {}
            for name in names:
                try:
                    raw = read_relative(root, name, remaining)
                except FileNotFoundError:
                    raw = b""
                result[name] = raw
                remaining -= len(raw)
                if name.endswith(".jsonl") and raw and not raw.endswith(b"\n"):
                    raise ValueError("Memory history has an incomplete tail")
            return result

        def scoped(value, model):
            item = model.model_validate(value)
            if item.scope != scope or item.scope_id != scope_id:
                raise ValueError("Memory record is outside the exported scope")
            return item

        with self._lock_for(f"{scope}:{scope_id}"):
            raw = capture()
            tombstones = [scoped(parse_json(line), MemoryTombstone)
                          for line in raw["tombstones.jsonl"].splitlines() if line.strip()]
            deleted = {item.memory_id for item in tombstones}
            items = [scoped(value, MemoryItem) for value in parse_json(raw["items.json"] or b"[]")]
            history = []
            for line in raw["history.jsonl"].splitlines():
                if not line.strip():
                    continue
                value = parse_json(line)
                if not isinstance(value, dict) or set(value) != {"at", "action", "item"}:
                    raise ValueError("Memory history is invalid")
                item = scoped(value["item"], MemoryItem)
                if not isinstance(value["at"], str) or not isinstance(value["action"], str):
                    raise ValueError("Memory history is invalid")
                if item.id not in deleted:
                    history.append({"at": value["at"], "action": value["action"],
                                    "item": item.model_dump(mode="json")})
            if raw != capture():
                raise ValueError("Memory changed during export")
            return {"items": [item.model_dump(mode="json") for item in items
                              if item.id not in deleted],
                    "history": history,
                    "tombstones": [item.model_dump(mode="json") for item in tombstones]}

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

    def _append_tombstone(self, tombstone: MemoryTombstone) -> None:
        path = self._tombstones_path(tombstone.scope, tombstone.scope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(tombstone.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

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
        temporary = path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            if retained:
                handle.write("\n".join(retained) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

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
            path.write_text("\n".join(retained) + "\n", encoding="utf-8")

    def _recover_hard_deletions(self) -> None:
        """Finish content scrubbing after a crash once a tombstone is durable."""

        for scope in MemoryScope:
            scope_root = self.root / scope.value
            if not scope_root.exists():
                continue
            for scope_dir in (path for path in scope_root.iterdir() if path.is_dir()):
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
                "principal_role": (
                    principal.principal_role if isinstance(principal, RunRecord) else principal.role
                ),
                "principal_session_id": (
                    principal.principal_session_id
                    if isinstance(principal, RunRecord) else principal.session_id
                ),
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
            if memory_is_expired(current):
                raise ValueError("expired memory requires a future expiry before approval")
            existing = self.list(scope=current.scope, scope_id=current.scope_id)
            approved = [
                item
                for item in existing
                if item.status == MemoryStatus.APPROVED
                and item.id != current.id
                and not memory_is_expired(item)
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
        if memory_is_expired(current):
            raise ValueError("expired memory requires a future expiry before correction")
        existing = self.list(scope=current.scope, scope_id=current.scope_id)
        approved = [
            item
            for item in existing
            if item.status == MemoryStatus.APPROVED
            and item.id != current.id
            and not memory_is_expired(item)
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

    def set_expiry(self, memory_id: str, expires_at: datetime | None) -> MemoryItem:
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("memory expiry must be timezone-aware")
            if expires_at <= utc_now():
                raise ValueError("memory expiry must be in the future")

        def mutate(item: MemoryItem) -> MemoryItem:
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
            if any(
                item.memory_id == memory_id
                for item in self._read_tombstones(current.scope, current.scope_id)
            ):
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
