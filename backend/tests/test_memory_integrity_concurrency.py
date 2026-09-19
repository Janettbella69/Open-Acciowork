"""An expired read must not erase a concurrently renewed Agent mirror."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from ophira_backend.constants import MemoryKind, MemoryScope, MemorySource
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import utc_now


def test_expired_listing_and_renewal_preserve_current_agent_marker(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "agent_fixture").mkdir(parents=True)
    first = FileMemoryProvider(tmp_path / "memory", agents)
    second = FileMemoryProvider(tmp_path / "memory", agents)
    item = first.propose(
        scope=MemoryScope.AGENT, scope_id="agent_fixture", text="retain renewed fact",
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )
    first.decide(item.id, "approve")
    first.set_expiry(item.id, utc_now() + timedelta(minutes=1))
    later = utc_now() + timedelta(hours=1)
    monkeypatch.setattr("ophira_backend.memory.utc_now", lambda: later)
    entered, release, renewing, renewed = Event(), Event(), Event(), Event()
    original = first._sync_memory_md

    def pause_expired_sync(value):
        entered.set()
        assert release.wait(5)
        original(value)

    monkeypatch.setattr(first, "_sync_memory_md", pause_expired_sync)

    def renew():
        renewing.set()
        result = second.set_expiry(item.id, later + timedelta(days=1))
        renewed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        listing = executor.submit(first.list)
        assert entered.wait(5)
        renewal = executor.submit(renew)
        try:
            assert renewing.wait(5)
            renewed.wait(0.2)
        finally:
            release.set()
        listing.result(timeout=5)
        current = renewal.result(timeout=5)
    assert current.expires_at == later + timedelta(days=1)
    assert "retain renewed fact" in (agents / "agent_fixture" / "MEMORY.md").read_text()
