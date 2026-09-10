"""Crash receipts, process exclusion, immutable bytes and reviewed selections."""

import multiprocessing

import pytest
from test_auth_api import principal
from test_skill_repository import review_request, service, upload
from test_skills import skill_zip

from ophira_backend import skill_repository as repository
from ophira_backend.auth import AuthAssertionExpiredError
from ophira_backend.constants import PrincipalRole
from ophira_backend.skill_import_models import SkillImportReviewRequest, SkillRepositorySelection
from ophira_backend.skills import SkillConflictError, SkillIntegrityError, SkillNotFoundError
from ophira_backend.storage import LedgerNotFoundError


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("committed", [False, True])
def test_restart_reconciles_the_receipt_without_republishing(tmp_path, monkeypatch, committed):
    store = service(tmp_path)
    detail = upload(store)
    identifier = detail["import"]["id"]
    request = review_request(detail)

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(
            store if committed else store.registry,
            "finish" if committed else "publish_reviewed_candidate",
            crash,
        )
        with pytest.raises(SimulatedCrash):
            store.review(identifier, request, None)
    assert store.load(identifier).state == "publishing"
    newer = store.registry.install_zip(skill_zip(version="3.0.0", body="Newer admin selection"))
    store.registry.set_enabled(newer.id, True)
    token = store.registry.selection_token(newer.id)
    restarted = service(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(restarted.registry, "publish_reviewed_candidate", crash)
        result = restarted.detail(identifier, None)
        assert result["import"]["state"] == ("published" if committed else "pending")
        if committed:
            assert restarted.review(identifier, request, None) == result
        else:
            with pytest.raises(SkillConflictError, match="reload and review again"):
                restarted.review(identifier, request, None)
    assert restarted.registry.selection_token(newer.id) == token
    assert len(result["import"]["history"]) == 1


def test_expired_assertion_after_scan_cannot_commit_publication(tmp_path, monkeypatch):
    store = service(tmp_path, trusted=True)
    actor = principal("owner", PrincipalRole.OWNER)
    detail = upload(store, actor)
    original = store.registry.publish_reviewed_candidate

    def expire_before_commit(*args, **kwargs):
        monkeypatch.setattr(repository, "utc_now", lambda: actor.assertion_expires_at)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.registry, "publish_reviewed_candidate", expire_before_commit)
    with pytest.raises(AuthAssertionExpiredError):
        store.review(detail["import"]["id"], review_request(detail), actor)
    assert store.registry.publication_receipt(detail["import"]["id"]) is None
    assert store.load(detail["import"]["id"]).state == "pending"


def test_selection_checks_revision_and_deduplicates_only_immediate_retry(tmp_path):
    store = service(tmp_path)
    first = upload(store)
    store.review(first["import"]["id"], review_request(first), None)
    skill = first["import"]["skill"]
    enable = SkillRepositorySelection(
        operation="enable",
        content_hash=skill["content_hash"],
        expected_target=store.registry.selection_token(skill["id"]),
    )
    changed = store.select(skill["name"], enable, None)
    assert changed["skill"]["enabled"] is True
    assert store.select(skill["name"], enable, None) == changed
    store.registry.set_enabled(skill["id"], False)
    with pytest.raises(SkillConflictError):
        store.select(skill["name"], enable, None)
    second = upload(store, version="2.0.0")
    store.review(second["import"]["id"], review_request(second), None)
    restore = SkillRepositorySelection(
        operation="rollback",
        content_hash=skill["content_hash"],
        expected_target=store.registry.selection_token(skill["id"]),
    )
    restored = store.select(skill["name"], restore, None)
    assert restored["skill"]["content_hash"] == skill["content_hash"]
    assert restored["skill"]["enabled"] is False
    assert store.select(skill["name"], restore, None) == restored


@pytest.mark.parametrize("operation", ["enable", "rollback"])
def test_expired_selection_guard_keeps_canonical_state(tmp_path, monkeypatch, operation):
    store = service(tmp_path, trusted=True)
    actor = principal("admin", PrincipalRole.ADMIN)
    detail = upload(store, actor)
    store.review(detail["import"]["id"], review_request(detail), actor)
    skill = detail["import"]["skill"]
    token = store.registry.selection_token(skill["id"])
    original = store.registry._write_registry_state_unlocked

    def should_not_commit(*args, **kwargs):
        pytest.fail("Expired selection reached canonical commit")

    # Hold the Registry lock with a guard which expires just before its mutation.
    authorize = store.authorize
    calls = 0

    def expire_at_guard(principal, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(repository, "utc_now", lambda: actor.assertion_expires_at)
            monkeypatch.setattr(store.registry, "_write_registry_state_unlocked", should_not_commit)
        return authorize(principal, **kwargs)

    monkeypatch.setattr(store, "authorize", expire_at_guard)
    with pytest.raises(AuthAssertionExpiredError):
        store.select(
            skill["name"],
            SkillRepositorySelection(
                operation=operation,
                content_hash=skill["content_hash"],
                expected_target=token,
            ),
            actor,
        )
    monkeypatch.setattr(store.registry, "_write_registry_state_unlocked", original)
    assert store.registry.selection_token(skill["id"]) == token


@pytest.mark.parametrize("damage", ["bytes", "symlink", "hardlink"])
def test_changed_or_linked_import_file_is_never_published(tmp_path, damage):
    store = service(tmp_path)
    detail = upload(store)
    package = store.path(detail["import"]["id"]) / "package"
    instruction = package / "SKILL.md"
    original = instruction.read_bytes()
    package.chmod(0o755)
    instruction.chmod(0o644)
    instruction.unlink()
    if damage == "bytes":
        instruction.write_bytes(original + b"\nchanged")
    else:
        external = tmp_path / "outside.md"
        external.write_bytes(original)
        if damage == "symlink":
            instruction.symlink_to(external)
        else:
            instruction.hardlink_to(external)
    with pytest.raises(SkillIntegrityError):
        store.review(detail["import"]["id"], review_request(detail), None)
    assert store.registry.publication_receipt(detail["import"]["id"]) is None


def _review_in_process(root, identifier, payload, queue):
    try:
        store = service(root)
        request = SkillImportReviewRequest.model_validate_json(payload)
        queue.put(store.review(identifier, request, None)["import"]["state"])
    except Exception as exc:
        queue.put(type(exc).__name__)


def test_process_retries_record_one_review_and_one_selection(tmp_path):
    store = service(tmp_path)
    detail = upload(store)
    identifier = detail["import"]["id"]
    request = review_request(detail).model_dump_json()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_review_in_process, args=(tmp_path, identifier, request, queue))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert [queue.get(timeout=5) for _ in processes] == ["published", "published"]
        item = store.load(identifier)
        assert item.state == "published" and len(item.history) == 1
        assert store.registry.selection_token(item.skill.id).generation == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_unpublished_staging_is_cleaned_under_the_import_lock(tmp_path):
    store = service(tmp_path)
    detail = upload(store)
    stage = store.root / ".import-abandoned"
    stage.mkdir()
    (stage / "partial.md").write_text("Interrupted upload")
    (stage / "partial.md").chmod(0o444)
    stage.chmod(0o555)
    assert store.list_imports(None)[0]["import"]["id"] == detail["import"]["id"]
    assert not stage.exists()


def test_archive_without_commit_receipt_is_hidden_from_history_export_and_restore(
    tmp_path,
    monkeypatch,
):
    store = service(tmp_path)
    detail = upload(store)
    item = detail["import"]

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(store.registry, "_write_registry_state_unlocked", crash)
        with pytest.raises(SimulatedCrash):
            store.review(item["id"], review_request(detail), None)
    # The on-disk archive exists; neither API history nor export treats it as published.
    assert store.registry.versions(item["skill"]["id"])
    for restarted in [store, service(tmp_path)]:
        assert restarted.versions(item["skill"]["id"], None) == []
        with pytest.raises(LedgerNotFoundError):
            restarted.export(item["skill"]["name"], item["skill"]["content_hash"], None)
        with pytest.raises(SkillNotFoundError):
            restarted.rollback(
                item["skill"]["id"], content_hash=item["skill"]["content_hash"], principal=None
            )
    refreshed = store.detail(item["id"], None)
    assert refreshed["import"]["state"] == "pending"
    assert store.versions(item["skill"]["id"], None) == []
    store.review(item["id"], review_request(refreshed), None)
    assert (
        store.versions(item["skill"]["id"], None)[0].content_hash == item["skill"]["content_hash"]
    )


def test_reimporting_existing_operator_history_does_not_hide_published_files(tmp_path):
    store = service(tmp_path)
    old = store.registry.install_zip(skill_zip())
    store.registry.install_zip(skill_zip(version="2.0.0", body="Updated method"))
    detail = upload(store)
    assert detail["import"]["published_before_import"] is True
    assert len(store.versions(old.id, None)) == 2
    assert store.export(old.name, old.content_hash, None)[1]
