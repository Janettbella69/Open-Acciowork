"""SkillRegistry: discovery from the application Plugin, versioning, and enable state."""

from pathlib import Path

import pytest

from ophira_backend.config import get_settings
from ophira_backend.skills import SkillRegistry

REAL_PLUGIN_DIR = get_settings().capability_plugin_dir

EXPECTED_SKILLS = {
    "ophira-core:global-opportunity-execution",
    "ophira-core:supplier-due-diligence",
    "ophira-core:landed-cost-analysis",
    "ophira-core:customer-path",
}


def build_registry(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry(REAL_PLUGIN_DIR, tmp_path / "skills-state")


def test_registry_discovers_the_four_shipped_plugin_skills(tmp_path: Path) -> None:
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
    assert len(selection) == 3

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
    with pytest.raises(ValueError, match="frontmatter"):
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
    with pytest.raises(ValueError, match="namespace"):
        registry.list()


def test_unknown_skill_raises_not_found(tmp_path: Path) -> None:
    from ophira_backend.skills import SkillNotFoundError

    with pytest.raises(SkillNotFoundError):
        build_registry(tmp_path).get("ophira-core:missing")
