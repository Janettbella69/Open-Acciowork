"""Channel binding writes cannot survive a committed project deletion intent."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_channel_identity import configured_store, create_payload

from ophira_backend.channel_identity import ChannelIdentityNotFoundError
from ophira_backend.project_lifecycle import ProjectLifecycleBusyError


def test_channel_write_holds_project_lease_through_atomic_persistence(tmp_path, monkeypatch):
    store, access, owner = configured_store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = store._write_available

    def paused(record, *, create=False):
        entered.set()
        assert release.wait(5)
        return original(record, create=create)

    monkeypatch.setattr(store, "_write_available", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.create, create_payload(), actor=owner)
        try:
            assert entered.wait(5)
            with (
                pytest.raises(ProjectLifecycleBusyError),
                access.lifecycle.lease(
                    "project_123",
                    exclusive=True,
                ),
            ):
                pass
        finally:
            release.set()
        assert future.result().project_id == "project_123"


def test_deleted_channel_binding_cannot_be_listed_retrieved_or_written(tmp_path):
    store, access, owner = configured_store(tmp_path)
    binding = store.create(create_payload(), actor=owner)
    directory = access.lifecycle.root / "project-deletions/project_123"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text("{}")
    assert store.list_for_tenant(owner.tenant_id, include_revoked=True) == []
    with pytest.raises(ChannelIdentityNotFoundError):
        store.get_for_tenant(binding.id, owner.tenant_id)
    with pytest.raises(ChannelIdentityNotFoundError):
        store._write(binding)
    with pytest.raises(ChannelIdentityNotFoundError):
        store.create(create_payload(), actor=owner)
