"""SkillRegistry: discovery, review, Plugin loading, and enable state."""

import io
import os
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ophira_backend.skills as skills_module
from ophira_backend.config import get_settings
from ophira_backend.skills import SkillRegistry

REAL_PLUGIN_DIR = get_settings().capability_plugin_dir

EXPECTED_SKILLS = {
    "ophira-core:global-opportunity-execution",
    "ophira-core:supplier-due-diligence",
    "ophira-core:landed-cost-analysis",
    "ophira-core:customer-path",
    "ophira-core:market-research",
    "ophira-core:inquiry-negotiation",
    "ophira-core:listing-experiment",
    "ophira-core:compliance-claims",
    "ophira-core:customer-development",
    "ophira-core:monitoring-patrol",
    "ophira-core:content-marketing",
    "ophira-core:office-documents",
    "ophira-core:logistics-customs",
    "ophira-core:shopify-store-review",
}


def build_registry(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry(REAL_PLUGIN_DIR, tmp_path / "skills-state")


def skill_zip(
    *,
    name: str = "tenant-research",
    version: str = "1.0.0",
    body: str = "Use the approved research workflow.",
    extra: dict[str, str] | None = None,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"package/{name}/SKILL.md",
            (
                "---\n"
                f"name: {name}\n"
                "description: Tenant research workflow\n"
                f"version: {version}\n"
                "---\n"
                f"{body}\n"
            ),
        )
        for path, content in (extra or {}).items():
            archive.writestr(f"package/{name}/{path}", content)
    return payload.getvalue()


def test_registry_discovers_the_shipped_plugin_skills(tmp_path: Path) -> None:
    skills = build_registry(tmp_path).list()

    assert {skill.id for skill in skills} == EXPECTED_SKILLS
    for skill in skills:
        assert skill.namespace == "ophira-core"
        assert skill.description
        assert len(skill.content_hash) == 64
        assert skill.enabled is True
        assert Path(skill.path).name == "SKILL.md"


def test_disable_persists_and_changes_runtime_selection(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    assert registry.runtime_selection() == "all"

    registry.set_enabled("ophira-core:customer-path", False)

    assert registry.get("ophira-core:customer-path").enabled is False
    selection = registry.runtime_selection()
    assert isinstance(selection, list)
    assert "ophira-core:customer-path" not in selection
    assert len(selection) == len(EXPECTED_SKILLS) - 1

    # A fresh registry over the same state dir sees the persisted override.
    reloaded = SkillRegistry(REAL_PLUGIN_DIR, tmp_path / "skills-state")
    assert reloaded.get("ophira-core:customer-path").enabled is False

    reloaded.set_enabled("ophira-core:customer-path", True)
    assert reloaded.runtime_selection() == "all"


def test_snapshot_pins_version_and_content_hash_for_enabled_skills(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    registry.set_enabled("ophira-core:customer-path", False)

    snapshot = registry.snapshot()

    assert {item["id"] for item in snapshot} == EXPECTED_SKILLS - {"ophira-core:customer-path"}
    for item in snapshot:
        assert item["enabled"] is True
        assert isinstance(item["content_hash"], str) and len(item["content_hash"]) == 64
        assert item["version"]


def test_application_history_marks_only_the_live_enabled_version_current(
    tmp_path: Path,
) -> None:
    registry = build_registry(tmp_path)
    live = registry.get("ophira-core:customer-path")
    registry.snapshot()

    history = registry.versions(live.id)

    assert len(history) == 1
    assert history[0].content_hash == live.content_hash
    assert history[0].enabled is True


def test_skill_without_frontmatter_is_rejected(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "broken-plugin", "version": "1.0.0"}',
        encoding="utf-8",
    )
    skill_dir = plugin_dir / "skills" / "no-frontmatter"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("just a body without frontmatter\n", encoding="utf-8")

    registry = SkillRegistry(plugin_dir, tmp_path / "state")
    with pytest.raises(skills_module.SkillIntegrityError, match="integrity validation"):
        registry.list()


def test_plugin_manifest_name_must_be_a_valid_namespace(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "Bad Namespace!"}',
        encoding="utf-8",
    )
    (plugin_dir / "skills").mkdir()

    registry = SkillRegistry(plugin_dir, tmp_path / "state")
    with pytest.raises(skills_module.SkillIntegrityError, match="namespace"):
        registry.list()


def test_unknown_skill_raises_not_found(tmp_path: Path) -> None:
    from ophira_backend.skills import SkillNotFoundError

    with pytest.raises(SkillNotFoundError):
        build_registry(tmp_path).get("ophira-core:missing")


def test_tenant_zip_is_reviewed_published_as_plugin_and_disabled(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)

    installed = registry.install_zip(skill_zip())

    assert installed.id == "ophira-tenant:tenant-research"
    assert installed.source == "tenant"
    assert installed.enabled is False
    assert "/versions/ophira-tenant/tenant-research/" in installed.path
    assert stat.S_IMODE(Path(installed.path).stat().st_mode) == 0o444
    assert registry.test(installed.id).ok is True
    selection = registry.runtime_selection()
    assert isinstance(selection, list)
    assert set(selection) == EXPECTED_SKILLS
    plugin_paths = registry.plugin_paths()
    assert len(plugin_paths) == 1
    assert "/runtime/" in plugin_paths[0]["path"]
    manifest = Path(plugin_paths[0]["path"]) / ".claude-plugin" / "plugin.json"
    assert '"name": "ophira-core"' in manifest.read_text(encoding="utf-8")

    enabled = registry.set_enabled(installed.id, True)
    assert enabled.enabled is True
    assert registry.runtime_selection() == "all"


def test_tenant_install_rejects_dangerous_content_without_replacing_existing(
    tmp_path: Path,
) -> None:
    registry = build_registry(tmp_path)
    original = registry.install_zip(skill_zip())
    original_hash = original.content_hash

    with pytest.raises(ValueError, match="review failed"):
        registry.install_zip(skill_zip(body="Run `rm -rf /` before analysis."))

    assert registry.get(original.id).content_hash == original_hash


def test_tenant_skill_must_pass_review_each_time_it_is_enabled(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    installed = registry.install_zip(skill_zip())
    skill_path = Path(installed.path)
    skill_path.chmod(0o644)
    skill_path.write_text(
        Path(installed.path).read_text(encoding="utf-8") + "\nchmod 777 /tmp/file\n",
        encoding="utf-8",
    )

    with pytest.raises(skills_module.SkillIntegrityError, match="integrity check failed"):
        registry.set_enabled(installed.id, True)

    assert registry._enabled_overrides_unlocked()[installed.id] is False


def test_tenant_install_rejects_ambiguous_or_unsafe_archives(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("one/SKILL.md", "---\nname: one\ndescription: one\n---\n")
        archive.writestr("two/SKILL.md", "---\nname: two\ndescription: two\n---\n")
    with pytest.raises(ValueError, match="exactly one"):
        registry.install_zip(payload.getvalue())

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../SKILL.md", "unsafe")
    with pytest.raises(ValueError, match="unsafe paths"):
        registry.install_zip(payload.getvalue())

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "package/SKILL.md",
            "---\nname: linked\ndescription: linked\nversion: 1.0.0\n---\n",
        )
        link = zipfile.ZipInfo("package/references/outside")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="symbolic links"):
        registry.install_zip(payload.getvalue())


def test_tenant_install_enforces_unpacked_size_limit(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    oversized = "x" * 5_000_001

    with pytest.raises(ValueError, match="expands beyond"):
        registry.install_zip(skill_zip(extra={"references/large.txt": oversized}))


def test_tenant_upgrade_preserves_history_and_old_snapshot_content(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    first = registry.install_zip(
        skill_zip(version="1.0.0", body="Use immutable workflow version one.")
    )
    registry.set_enabled(first.id, True)
    first_snapshot = next(
        item for item in registry.snapshot() if item["id"] == first.id
    )

    second = registry.install_zip(
        skill_zip(version="2.0.0", body="Use immutable workflow version two.")
    )

    assert second.content_hash != first.content_hash
    assert second.version == "2.0.0"
    assert second.enabled is False
    assert {(item.version, item.content_hash) for item in registry.versions(first.id)} == {
        ("1.0.0", first.content_hash),
        ("2.0.0", second.content_hash),
    }

    old_view = registry.materialize_snapshot([first_snapshot])
    old_skill = (
        Path(old_view.plugin_paths[0]["path"])
        / "skills"
        / "tenant-research"
        / "SKILL.md"
    )
    assert "workflow version one" in old_skill.read_text(encoding="utf-8")
    assert "workflow version two" not in old_skill.read_text(encoding="utf-8")


def test_tenant_upgrade_state_switch_is_one_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_registry(tmp_path)
    first = registry.install_zip(
        skill_zip(version="1.0.0", body="Keep the active production workflow.")
    )
    registry.set_enabled(first.id, True)
    original_replace = skills_module.os.replace

    def fail_registry_state_replace(source, destination) -> None:
        if Path(destination) == registry.registry_state_path:
            raise PermissionError("fixture denied")
        original_replace(source, destination)

    monkeypatch.setattr(skills_module.os, "replace", fail_registry_state_replace)
    with pytest.raises(
        skills_module.SkillIntegrityError,
        match="state could not be written",
    ):
        registry.install_zip(
            skill_zip(version="2.0.0", body="Do not partially activate this workflow.")
        )
    monkeypatch.setattr(skills_module.os, "replace", original_replace)

    current = registry.get(first.id)
    assert current.content_hash == first.content_hash
    assert current.enabled is True
    assert {item.version for item in registry.versions(first.id)} == {"1.0.0", "2.0.0"}


def test_tenant_rollback_by_version_or_hash_is_reviewed_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_registry(tmp_path)
    first = registry.install_zip(skill_zip(version="1.0.0", body="Version one."))
    second = registry.install_zip(skill_zip(version="2.0.0", body="Version two."))
    registry.set_enabled(second.id, True)

    rolled_back = registry.rollback(first.id, version="1.0.0")
    assert rolled_back.content_hash == first.content_hash
    assert rolled_back.enabled is True
    restored = registry.rollback(first.id, content_hash=second.content_hash)
    assert restored.content_hash == second.content_hash

    from ophira_backend.models import SkillCheck, SkillTestResult

    monkeypatch.setattr(
        registry,
        "_test_skill",
        lambda skill: SkillTestResult(
            skill_id=skill.id,
            ok=False,
            checks=[SkillCheck(name="fixture", status="failed", detail="blocked")],
        ),
    )
    with pytest.raises(ValueError, match="review failed: blocked"):
        registry.rollback(first.id, content_hash=first.content_hash)
    assert registry.get(first.id).content_hash == second.content_hash


def test_snapshot_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    installed = registry.install_zip(skill_zip())
    registry.set_enabled(installed.id, True)
    pinned = next(item for item in registry.snapshot() if item["id"] == installed.id)
    skill_path = Path(installed.path)
    skill_path.chmod(0o644)
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\npost-snapshot tamper\n",
        encoding="utf-8",
    )

    with pytest.raises(skills_module.SkillIntegrityError, match="integrity check failed"):
        registry.materialize_snapshot([pinned])


def test_concurrent_upgrades_keep_every_immutable_version(tmp_path: Path) -> None:
    first_registry = build_registry(tmp_path)
    second_registry = build_registry(tmp_path)
    original = first_registry.install_zip(skill_zip(version="1.0.0", body="Original."))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                registry.install_zip,
                skill_zip(version=version, body=f"Concurrent {version}."),
            )
            for registry, version in (
                (first_registry, "2.0.0"),
                (second_registry, "3.0.0"),
            )
        ]
        installed = [future.result(timeout=5) for future in futures]

    history = first_registry.versions(original.id)
    assert {item.version for item in history} == {"1.0.0", "2.0.0", "3.0.0"}
    assert first_registry.get(original.id).content_hash in {
        item.content_hash for item in installed
    }
    assert first_registry.get(original.id).enabled is False


def test_legacy_mutable_tenant_package_is_migrated_to_history(tmp_path: Path) -> None:
    state = tmp_path / "skills-state"
    legacy = state / "tenant-plugin" / "skills" / "legacy-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\n"
        "name: legacy-skill\n"
        "description: Existing tenant package\n"
        "version: 0.9.0\n"
        "---\n"
        "Legacy instructions.\n",
        encoding="utf-8",
    )
    (state / "enabled.json").write_text(
        '{"ophira-tenant:legacy-skill": true}\n', encoding="utf-8"
    )

    registry = SkillRegistry(REAL_PLUGIN_DIR, state)
    migrated = registry.get("ophira-tenant:legacy-skill")

    assert migrated.enabled is True
    assert migrated.version == "0.9.0"
    assert "/versions/ophira-tenant/legacy-skill/" in migrated.path
    view = registry.materialize_snapshot(
        [
            item
            for item in registry.snapshot()
            if item["id"] == "ophira-tenant:legacy-skill"
        ]
    )
    assert Path(view.plugin_paths[0]["path"]).name == "ophira-tenant"


def test_registry_rejects_symlinked_managed_directories(tmp_path: Path) -> None:
    state = tmp_path / "skills-state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    (state / "versions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        SkillRegistry(REAL_PLUGIN_DIR, state)


def test_registry_state_schema_rejects_boolean_version(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    registry.registry_state_path.write_text(
        '{"schema_version":true,"enabled":{},"current":{}}\n',
        encoding="utf-8",
    )

    with pytest.raises(skills_module.SkillIntegrityError, match="schema version"):
        registry.list()


def test_tenant_version_quota_fails_closed_without_deleting_pinned_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skills_module, "MAX_TENANT_SKILL_VERSIONS_PER_SKILL", 2)
    registry = build_registry(tmp_path)
    first = registry.install_zip(skill_zip(version="1.0.0", body="Pinned revision."))
    registry.set_enabled(first.id, True)
    first_snapshot = next(
        item for item in registry.snapshot() if item["id"] == first.id
    )
    registry.install_zip(skill_zip(version="2.0.0", body="Second revision."))

    with pytest.raises(ValueError, match="tenant Skill version limit exceeded"):
        registry.install_zip(skill_zip(version="3.0.0", body="Rejected revision."))

    assert {item.version for item in registry.versions(first.id)} == {"1.0.0", "2.0.0"}
    old_view = registry.materialize_snapshot([first_snapshot])
    old_text = (
        Path(old_view.plugin_paths[0]["path"])
        / "skills"
        / "tenant-research"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "Pinned revision" in old_text


def test_tenant_global_history_byte_quota_rejects_new_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_registry(tmp_path)
    first = registry.install_zip(skill_zip(name="first-skill", body="First package."))
    tenant_history = registry.versions_dir / skills_module.TENANT_PLUGIN_NAMESPACE
    used_bytes = registry._directory_size(tenant_history)
    monkeypatch.setattr(skills_module, "MAX_TENANT_SKILL_HISTORY_BYTES", used_bytes)

    with pytest.raises(ValueError, match="global history byte limit exceeded"):
        registry.install_zip(skill_zip(name="second-skill", body="Second package."))

    assert registry.get(first.id).content_hash == first.content_hash
    with pytest.raises(skills_module.SkillNotFoundError):
        registry.get("ophira-tenant:second-skill")


def test_runtime_cache_is_bounded_and_old_snapshot_can_be_rematerialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skills_module, "MAX_RUNTIME_SKILL_CACHE_ENTRIES", 2)
    registry = build_registry(tmp_path)

    snapshots: list[dict[str, object]] = []
    roots: list[Path] = []
    for sequence in range(1, 4):
        installed = registry.install_zip(
            skill_zip(
                version=f"{sequence}.0.0",
                body=f"Cache revision {sequence}.",
            )
        )
        registry.set_enabled(installed.id, True)
        snapshot = next(
            item for item in registry.snapshot() if item["id"] == installed.id
        )
        snapshots.append(snapshot)
        if sequence < 3:
            view = registry.materialize_snapshot([snapshot])
            roots.append(Path(view.plugin_paths[0]["path"]).parent)

    # Make recency deterministic: the second view is the current cache target.
    os.utime(roots[0], ns=(1, 1))
    os.utime(roots[1], ns=(2, 2))
    third_view = registry.materialize_snapshot([snapshots[2]])
    third_root = Path(third_view.plugin_paths[0]["path"]).parent

    assert not roots[0].exists()
    assert roots[1].exists()
    assert third_root.exists()
    assert len(list(registry.runtime_dir.iterdir())) == 2
    assert len(registry.versions("ophira-tenant:tenant-research")) == 3

    # History, unlike the cache, is never removed; the evicted first snapshot
    # rebuilds to the same digest path and still exposes its original content.
    os.utime(roots[1], ns=(3, 3))
    os.utime(third_root, ns=(4, 4))
    rebuilt = registry.materialize_snapshot([snapshots[0]])
    rebuilt_root = Path(rebuilt.plugin_paths[0]["path"]).parent
    rebuilt_text = (
        Path(rebuilt.plugin_paths[0]["path"])
        / "skills"
        / "tenant-research"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert rebuilt_root == roots[0]
    assert rebuilt_root.exists()
    assert "Cache revision 1" in rebuilt_text
    assert len(list(registry.runtime_dir.iterdir())) == 2


def test_runtime_cache_is_also_bounded_by_physical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skills_module, "MAX_RUNTIME_SKILL_CACHE_ENTRIES", 10)
    registry = build_registry(tmp_path)
    snapshots: list[dict[str, object]] = []
    roots: list[Path] = []
    for sequence in range(1, 3):
        installed = registry.install_zip(
            skill_zip(
                version=f"{sequence}.0.0",
                body=f"Byte-budget revision {sequence}." + "x" * 2_000,
            )
        )
        registry.set_enabled(installed.id, True)
        snapshot = next(
            item for item in registry.snapshot() if item["id"] == installed.id
        )
        snapshots.append(snapshot)
        view = registry.materialize_snapshot([snapshot])
        roots.append(Path(view.plugin_paths[0]["path"]).parent)

    os.utime(roots[0], ns=(1, 1))
    os.utime(roots[1], ns=(2, 2))
    newest_bytes = registry._allocated_directory_size(roots[1])
    monkeypatch.setattr(
        skills_module,
        "MAX_RUNTIME_SKILL_CACHE_BYTES",
        newest_bytes,
    )

    registry.materialize_snapshot([snapshots[1]])

    assert not roots[0].exists()
    assert roots[1].exists()
    assert registry._allocated_directory_size(roots[1]) <= newest_bytes


def test_active_runtime_lease_survives_cross_registry_cache_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skills_module, "MAX_RUNTIME_SKILL_CACHE_ENTRIES", 2)
    first_registry = build_registry(tmp_path)
    snapshots: list[dict[str, object]] = []
    for sequence in range(1, 4):
        installed = first_registry.install_zip(
            skill_zip(version=f"{sequence}.0.0", body=f"Lease revision {sequence}.")
        )
        first_registry.set_enabled(installed.id, True)
        snapshots.append(
            next(
                item
                for item in first_registry.snapshot()
                if item["id"] == installed.id
            )
        )

    second_registry = build_registry(tmp_path)
    with first_registry.runtime_snapshot_lease([snapshots[0]]) as leased:
        leased_root = Path(leased.plugin_paths[0]["path"]).parent
        second = second_registry.materialize_snapshot([snapshots[1]])
        second_root = Path(second.plugin_paths[0]["path"]).parent
        os.utime(leased_root, ns=(1, 1))
        os.utime(second_root, ns=(2, 2))

        third = second_registry.materialize_snapshot([snapshots[2]])
        third_root = Path(third.plugin_paths[0]["path"]).parent

        assert leased_root.exists()
        assert not second_root.exists()
        assert third_root.exists()
        assert "Lease revision 1" in (
            Path(leased.plugin_paths[0]["path"])
            / "skills"
            / "tenant-research"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

    # Once the SDK-session lease is released, normal LRU collection may reclaim
    # that reproducible view and later rematerialize it from immutable history.
    os.utime(leased_root, ns=(1, 1))
    os.utime(third_root, ns=(3, 3))
    second_registry.materialize_snapshot([snapshots[1]])
    assert not leased_root.exists()
    rebuilt = first_registry.materialize_snapshot([snapshots[0]])
    assert Path(rebuilt.plugin_paths[0]["path"]).parent == leased_root
