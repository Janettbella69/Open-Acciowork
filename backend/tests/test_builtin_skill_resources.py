"""Exercise shipped resource packages through isolated immutable Plugin views."""

import re
import shutil
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_skills import EXPECTED_SKILLS, REAL_PLUGIN_DIR

from ophira_backend.skills import SkillRegistry


def reachable_markdown(skill_root: Path) -> dict[Path, bytes]:
    """Follow the files the Agent can discover from a package's SKILL.md."""
    pending = [skill_root / "SKILL.md"]
    found: dict[Path, bytes] = {}
    while pending:
        document = pending.pop().resolve()
        assert document.is_relative_to(skill_root.resolve()), document
        relative = document.relative_to(skill_root.resolve())
        if relative in found:
            continue
        body = document.read_bytes()
        found[relative] = body
        for target in re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", body.decode("utf-8")):
            link = urlsplit(target)
            if link.scheme or link.netloc or not link.path.endswith(".md"):
                continue
            pending.append(document.parent / link.path)
    return found


@pytest.mark.parametrize("skill_id", sorted(EXPECTED_SKILLS))
def test_each_builtin_resource_package_survives_single_skill_snapshot_and_workspace_edits(
    tmp_path: Path, skill_id: str,
) -> None:
    # Only mutate the test copy of the application Plugin, never the source
    # package or existing user/runtime history.
    source_plugin = tmp_path / "application-plugin"
    shutil.copytree(REAL_PLUGIN_DIR, source_plugin)
    registry = SkillRegistry(source_plugin, tmp_path / "registry")
    original = next(item for item in registry.snapshot() if item["id"] == skill_id)
    name = skill_id.split(":", 1)[1]
    source_skill = source_plugin / "skills" / name
    expected = reachable_markdown(source_skill)
    references = [path for path in expected if path.parts[0] == "references"]
    templates = [path for path in expected if path.parts[0] == "templates"]
    assert references and templates
    assert set(expected) == {path.relative_to(source_skill) for path in source_skill.rglob("*.md")}
    assert registry.test(skill_id).ok

    # A later application edit must change the content-addressed pin without
    # rewriting resources belonging to an earlier Run.
    for relative in [references[0], templates[0]]:
        target = source_skill / relative
        target.write_bytes(expected[relative] + b"\nNew application revision for this test.\n")
    revised = next(item for item in registry.snapshot() if item["id"] == skill_id)
    assert revised["content_hash"] != original["content_hash"]

    with registry.runtime_snapshot_lease([original]) as view:
        assert view.skill_ids == [skill_id]
        plugin = Path(view.plugin_paths[0]["path"])
        assert {path.name for path in (plugin / "skills").iterdir()} == {name}
        pinned_skill = plugin / "skills" / name
        assert reachable_markdown(pinned_skill) == expected
        for relative in expected:
            assert stat.S_IMODE((pinned_skill / relative).stat().st_mode) & 0o222 == 0

        workspace = tmp_path / "project-workspace"
        workspace.mkdir()
        for index, relative in enumerate(templates):
            artifact = workspace / f"editable-{index}.md"
            shutil.copyfile(pinned_skill / relative, artifact)
            artifact.write_bytes(artifact.read_bytes() + b"\nUser project edit.\n")
            assert artifact.read_bytes() != expected[relative]
            assert (pinned_skill / relative).read_bytes() == expected[relative]

        with registry.runtime_snapshot_lease([revised]) as newer:
            revised_skill = Path(newer.plugin_paths[0]["path"]) / "skills" / name
            assert (revised_skill / references[0]).read_bytes() != expected[references[0]]
            assert (revised_skill / templates[0]).read_bytes() != expected[templates[0]]
            assert reachable_markdown(pinned_skill) == expected
