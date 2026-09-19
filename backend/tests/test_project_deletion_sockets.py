"""Socket cleanup never connects to a daemon or touches other shop endpoints."""

import socket
import tempfile
from pathlib import Path

import pytest

from ophira_backend.project_deletion_capture import ProjectDeletionConflict
from ophira_backend.project_deletion_sockets import ShopSocketPlan, erase, preview

SHOP = "shop_" + "a" * 32
OTHER = "shop_" + "b" * 32


@pytest.fixture
def environment(tmp_path, monkeypatch):
    # macOS limits Unix socket paths to 104 bytes; use a real short temp parent.
    with tempfile.TemporaryDirectory(prefix="pds-", dir="/tmp") as directory:
        monkeypatch.setattr(tempfile, "gettempdir", lambda: directory)
        root = tmp_path / "data"
        root.mkdir()
        yield root


def socket_file(plan, shop=SHOP):
    directory = Path(plan.directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (shop[-16:] + ".sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(path))
    return path


def test_exact_idle_socket_removed_and_other_shop_kept(environment):
    empty = preview(environment, [SHOP])
    first = socket_file(empty)
    other = socket_file(empty, OTHER)
    plan = ShopSocketPlan.model_validate_json(preview(environment, [SHOP]).model_dump_json())
    assert plan.entries[0].kind == "socket"
    assert erase(plan) == 1
    assert erase(plan) == 0
    assert not first.exists() and other.exists()


def test_locator_matches_real_shop_browser_store(environment):
    from ophira_backend.models import ProjectCreate
    from ophira_backend.shop_browser_store import ShopBrowserStore, ShopIdentity
    from ophira_backend.storage import FileLedger

    ledger = FileLedger(environment)
    project = ledger.create_project(ProjectCreate(name="Shop", objective="Socket fixture"))
    store = ShopBrowserStore(ledger)
    shop = ShopIdentity(
        id=SHOP,
        project_id=project.id,
        name="Fixture",
        seller_shop_id="fixture",
        platform="other",
        entry_url="https://seller.example.com",
        created_by="local_owner",
        tenant_id=None,
        identity_binding="local_only",
    )
    store.write(shop)
    path = store.socket_path(shop.id)
    plan = preview(environment, [shop.id])
    assert (Path(plan.directory) / plan.entries[0].name) == path.resolve()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(path))
    plan = preview(environment, [shop.id])
    assert plan.entries[0].kind == "socket"
    assert erase(plan) == 1
    assert not path.exists()


def test_service_erases_reviewed_socket_before_removing_shop_records(environment):
    from uuid import uuid4

    from ophira_backend.auth import ProjectAccessStore
    from ophira_backend.models import ProjectCreate
    from ophira_backend.project_deletion import ProjectDeletionService
    from ophira_backend.project_deletion_models import ProjectDeletionRequest
    from ophira_backend.shop_browser_store import ShopBrowserStore, ShopIdentity
    from ophira_backend.storage import FileLedger

    ledger = FileLedger(environment)
    project = ledger.create_project(ProjectCreate(name="Delete Shop", objective="Fixture"))
    store = ShopBrowserStore(ledger)
    shop = ShopIdentity(
        id=SHOP, project_id=project.id, name="Fixture", seller_shop_id="fixture",
        platform="other", entry_url="https://seller.example.com", created_by="local_owner",
        tenant_id=None, identity_binding="local_only",
    )
    store.write(shop)
    path = store.socket_path(shop.id)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(path))
    service = ProjectDeletionService(ledger, ProjectAccessStore(environment / "auth" / "projects"))
    reviewed = service.preview(project.id, None)
    assert any(item.id == "browser-sockets" and item.files == 1 for item in reviewed.domains)
    result = service.start(project.id, ProjectDeletionRequest(
        operation_id=str(uuid4()), fingerprint=reviewed.fingerprint,
        confirmation_name=project.name, reason="Remove orphaned test endpoint",
    ), None)
    assert result.status == "deleted", result
    assert not path.exists()
    assert not store.path(shop.id).exists()
    assert not (environment / "projects" / project.id).exists()


def test_late_or_replaced_socket_requires_new_review(environment):
    empty = preview(environment, [SHOP])
    path = socket_file(empty)
    with pytest.raises(ProjectDeletionConflict, match="changed"):
        erase(empty)
    plan = preview(environment, [SHOP])
    path.unlink()
    socket_file(plan)
    with pytest.raises(ProjectDeletionConflict, match="changed"):
        erase(plan)
    assert path.exists()


@pytest.mark.parametrize("kind", ["symlink", "regular", "directory"])
def test_non_socket_leaf_is_rejected_without_following_it(environment, kind):
    plan = preview(environment, [SHOP])
    directory = Path(plan.directory)
    directory.mkdir(mode=0o700)
    path = directory / (SHOP[-16:] + ".sock")
    target = environment / "retain.txt"
    target.write_bytes(b"not a socket")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "regular":
        path.write_bytes(b"not a socket")
    else:
        path.mkdir()
    with pytest.raises(ProjectDeletionConflict):
        preview(environment, [SHOP])
    with pytest.raises(ProjectDeletionConflict):
        erase(plan)
    assert target.read_bytes() == b"not a socket"


def test_linked_or_public_parent_is_rejected(environment):
    plan = preview(environment, [SHOP])
    directory = Path(plan.directory)
    target = environment / "retain-dir"
    target.mkdir()
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProjectDeletionConflict):
        preview(environment, [SHOP])
    directory.unlink()
    directory.mkdir(mode=0o755)
    with pytest.raises(ProjectDeletionConflict, match="private"):
        preview(environment, [SHOP])


def test_truncated_shop_filename_collision_is_rejected(environment):
    collision = "shop_" + "b" * 16 + "a" * 16
    with pytest.raises(ProjectDeletionConflict, match="ambiguous"):
        preview(environment, [SHOP, collision])
