from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ophira_backend.constants import (
    MemoryKind,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
)
from ophira_backend.memory import (
    FileMemoryProvider,
    MemoryNotFoundError,
    gather_approved,
)


def _user_memory(
    memory: FileMemoryProvider,
    text: str,
    *,
    scope: MemoryScope = MemoryScope.USER,
    scope_id: str = "default",
    expires_at: datetime | None = None,
):
    return memory.propose(
        scope=scope,
        scope_id=scope_id,
        text=text,
        kind=MemoryKind.FACT,
        source=MemorySource.USER,
        sensitivity=MemorySensitivity.SENSITIVE,
        created_by="owner",
        expires_at=expires_at,
    )


def test_expired_memory_is_excluded_from_recall_search_and_request_retrieval(
    tmp_path: Path,
) -> None:
    memory = FileMemoryProvider(tmp_path)
    now = datetime.now(UTC)
    expiring = _user_memory(memory, "Use EUR landed cost", expires_at=now + timedelta(hours=1))
    durable = _user_memory(memory, "Use EUR margin target")
    proposed = memory.propose(
        scope=MemoryScope.USER,
        scope_id="default",
        text="Use EUR unapproved draft",
        kind=MemoryKind.FACT,
        source=MemorySource.AGENT_PROPOSAL,
    )
    after_expiry = now + timedelta(hours=2)

    assert [item.id for item in memory.search("EUR", at=after_expiry)] == [durable.id]
    gathered = gather_approved(
        memory,
        [(MemoryScope.USER, "default")],
        query="EUR",
        at=after_expiry,
    )
    assert [item.id for item in gathered] == [durable.id]

    response = memory.retrieve(
        "EUR",
        pairs=[(MemoryScope.USER, "default")],
        identity=None,
        at=after_expiry,
    )
    assert [hit.item.id for hit in response.hits] == [durable.id]
    assert response.metadata.authorized_approved_candidates == 2
    assert response.metadata.excluded_expired == 1
    assert response.metadata.matched == 1
    assert proposed.id not in {hit.item.id for hit in response.hits}
    assert expiring.id not in {hit.item.id for hit in response.hits}


def test_request_retrieval_has_normalized_deterministic_match_and_ranking(
    tmp_path: Path,
) -> None:
    memory = FileMemoryProvider(tmp_path)
    exact = _user_memory(memory, "FOO bar foo")
    separated = _user_memory(memory, "bar notes then foo")

    response = memory.retrieve(
        "ＦＯＯ   BAR",
        pairs=[
            (MemoryScope.USER, "default"),
            (MemoryScope.USER, "default"),
        ],
        identity=None,
    )

    assert response.metadata.algorithm == "unicode_nfkc_casefold_all_terms_substring_v1"
    assert response.metadata.normalized_query == "foo bar"
    assert response.metadata.terms == ["foo", "bar"]
    assert response.metadata.requested_scope_pairs == ["user:default"]
    assert [hit.item.id for hit in response.hits] == [exact.id, separated.id]
    assert response.hits[0].rank == 1
    assert response.hits[0].exact_query_match is True
    assert response.hits[0].matched_terms == ["foo", "bar"]
    assert response.hits[0].term_occurrences == 3
    assert response.hits[1].exact_query_match is False


def test_hard_delete_scrubs_snapshots_history_and_agent_memory_marker(
    tmp_path: Path,
) -> None:
    agents_root = tmp_path / "agents"
    core_dir = agents_root / "agent_1"
    core_dir.mkdir(parents=True)
    memory_md = core_dir / "MEMORY.md"
    memory_md.write_text("# Long-term memory\n\nUser freehand note\n", encoding="utf-8")
    memory = FileMemoryProvider(tmp_path / "memory", agents_root=agents_root)
    secret_text = "private supplier negotiation threshold"
    item = _user_memory(
        memory,
        secret_text,
        scope=MemoryScope.AGENT,
        scope_id="agent_1",
    )
    memory.correct(item.id, text=f"updated {secret_text}")
    assert item.id in memory_md.read_text(encoding="utf-8")

    tombstone = memory.delete(
        item.id,
        deleted_by="owner",
        reason="User requested erasure",
    )

    with pytest.raises(MemoryNotFoundError):
        memory.get(item.id)
    assert memory.list(scope=MemoryScope.AGENT, scope_id="agent_1") == []
    assert tombstone.memory_id == item.id
    assert tombstone.content_sha256 == hashlib.sha256(
        f"updated {secret_text}".encode()
    ).hexdigest()
    assert tombstone.reason == "User requested erasure"
    assert memory.list_tombstones(scope=MemoryScope.AGENT, scope_id="agent_1") == [
        tombstone
    ]
    assert "User freehand note" in memory_md.read_text(encoding="utf-8")
    assert item.id not in memory_md.read_text(encoding="utf-8")

    scope_dir = tmp_path / "memory" / "agent" / "agent_1"
    assert secret_text not in (scope_dir / "items.json").read_text(encoding="utf-8")
    assert secret_text not in (scope_dir / "history.jsonl").read_text(encoding="utf-8")
    tombstone_text = (scope_dir / "tombstones.jsonl").read_text(encoding="utf-8")
    assert secret_text not in tombstone_text
    assert json.loads(tombstone_text)["memory_id"] == item.id


def test_tombstone_is_authoritative_and_startup_recovers_interrupted_scrub(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    memory = FileMemoryProvider(root)
    item = _user_memory(memory, "content that must be purged")
    tombstone = memory.delete(item.id, deleted_by="owner", reason="erase")
    scope_dir = root / "user" / "default"

    # Simulate a crash after the fsynced tombstone but before old content files
    # were replaced. The tombstone must hide it immediately and startup must scrub it.
    (scope_dir / "items.json").write_text(
        json.dumps([item.model_dump(mode="json")], default=str),
        encoding="utf-8",
    )
    (scope_dir / "history.jsonl").write_text(
        json.dumps({"action": "propose", "item": item.model_dump(mode="json")}, default=str)
        + "\n",
        encoding="utf-8",
    )
    assert memory.list(scope=MemoryScope.USER, scope_id="default") == []

    recovered = FileMemoryProvider(root)
    assert recovered.list(scope=MemoryScope.USER, scope_id="default") == []
    assert recovered.list_tombstones(scope=MemoryScope.USER, scope_id="default") == [
        tombstone
    ]
    assert "content that must be purged" not in (scope_dir / "items.json").read_text()
    assert "content that must be purged" not in (scope_dir / "history.jsonl").read_text()
    assert tombstone.memory_id in (scope_dir / "tombstones.jsonl").read_text()


def test_expiry_can_be_set_and_cleared_with_versioned_audit(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = _user_memory(memory, "temporary fact")
    expires_at = datetime.now(UTC) + timedelta(days=3)

    expiring = memory.set_expiry(item.id, expires_at)
    assert expiring.expires_at == expires_at
    assert expiring.version == 2
    permanent = memory.set_expiry(item.id, None)
    assert permanent.expires_at is None
    assert permanent.version == 3
    history = (tmp_path / "user" / "default" / "history.jsonl").read_text()
    assert "expiry.set" in history
    assert "expiry.clear" in history

    with pytest.raises(ValueError, match="future"):
        memory.set_expiry(item.id, datetime.now(UTC) - timedelta(seconds=1))


def test_observed_expiry_removes_only_the_generated_memory_md_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_root = tmp_path / "agents"
    core_dir = agents_root / "agent_1"
    core_dir.mkdir(parents=True)
    memory_md = core_dir / "MEMORY.md"
    memory_md.write_text("# Memory\n\nKeep this freehand note\n", encoding="utf-8")
    memory = FileMemoryProvider(tmp_path / "memory", agents_root=agents_root)
    now = datetime.now(UTC)
    item = _user_memory(
        memory,
        "temporary agent preference",
        scope=MemoryScope.AGENT,
        scope_id="agent_1",
        expires_at=now + timedelta(hours=1),
    )
    assert item.id in memory_md.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "ophira_backend.memory.utc_now",
        lambda: now + timedelta(hours=2),
    )
    assert memory.search("temporary") == []
    content = memory_md.read_text(encoding="utf-8")
    assert item.id not in content
    assert "Keep this freehand note" in content
