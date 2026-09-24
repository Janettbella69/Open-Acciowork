"""Official plugin packages reuse reviewed Skill and remote MCP boundaries."""

import io
import json
import stat
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, trusted_settings
from test_skill_repository_api import review_payload, signed

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.plugin_archives import inspect_plugin_archive


def plugin_zip(*, extra=None, manifest=None) -> bytes:
    manifest = manifest or {
        "name": "sample-plugin", "version": "1.2.0",
        "description": "Useful reviewed tools",
    }
    files = {
        "package/.claude-plugin/plugin.json": json.dumps(manifest),
        "package/skills/market-reader/SKILL.md": (
            "---\nname: market-reader\ndescription: Research market demand\n"
            "version: 1.2.0\n---\nRead sources and cite them.\n"
        ),
        "package/skills/price-check/SKILL.md": (
            "---\nname: price-check\ndescription: Compare product prices\n"
            "version: 1.2.0\n---\nCompare prices with evidence.\n"
        ),
        "package/.mcp.json": json.dumps({"mcpServers": {
            "remote": {"type": "http", "url": "https://mcp.example.com/mcp",
                       "headers": {"Authorization": "Bearer do-not-return"}},
            "local": {"command": "python", "args": ["danger.py"]},
        }}),
        "package/agents/researcher.md": "---\nname: researcher\n---\nAgent definition\n",
        "package/hooks/hooks.json": '{"hooks":{}}',
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_plugin_import_stages_each_skill_and_reports_mcp_and_ignored_agents(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        request_id = uuid4().hex
        path = f"/api/plugins/imports?request_id={request_id}"
        payload = plugin_zip()
        denied = await signed(client, "POST", path, PrincipalRole.MEMBER, content=payload)
        assert denied.status_code == 403
        staged = await signed(client, "POST", path, content=payload)
        assert staged.status_code == 201, staged.text
        result = staged.json()
        record = result["import"]
        assert record["name"] == "sample-plugin"
        assert len(record["skills"]) == 2
        assert {row["state"] for row in result["skill_states"]} == {"pending"}
        assert all(row["scan_ok"] for row in result["skill_states"])
        assert record["mcp_servers"] == [{
            "name": "remote", "url": "https://mcp.example.com/mcp",
            "credentials_required": True,
        }]
        assert record["ignored_agents"] == ["agents/researcher.md"]
        assert "hooks" in record["ignored_components"]
        assert any("local" in item for item in record["ignored_components"])
        assert "do-not-return" not in staged.text
        assert list((tmp_path / "user-mcp").rglob("*.json")) == []
        assert all(row["source"] != "tenant" for row in (
            await signed(client, "GET", "/api/skills", PrincipalRole.VIEWER)
        ).json())

        repeated = await signed(client, "POST", path, content=payload)
        assert repeated.json() == result
        assert (await signed(client, "POST", path, content=plugin_zip(
            extra={"package/CHANGELOG.md": "changed"},
        ))).status_code == 409
        identifier = record["id"]
        detail_path = f"/api/plugins/imports/{identifier}"
        assert (await signed(client, "GET", detail_path, PrincipalRole.MEMBER)).status_code == 403
        listed = await signed(client, "GET", "/api/plugins/imports")
        assert listed.json()[0]["id"] == identifier

        first = record["skills"][0]
        skill_path = f"/api/skills/imports/{first['import_id']}"
        skill_detail = (await signed(client, "GET", skill_path)).json()
        approved = await signed(
            client, "POST", skill_path + "/review", PrincipalRole.OWNER,
            json=review_payload(skill_detail),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["current_target"]["enabled"] is False
        skill = approved.json()["import"]["skill"]
        selection = await signed(
            client, "POST", f"/api/skills/repository/{skill['name']}/selection",
            json={
                "operation": "enable", "content_hash": skill["content_hash"],
                "expected_target": approved.json()["current_target"],
            },
        )
        assert selection.status_code == 200, selection.text
        runtime = app.state.skills.materialize_snapshot(app.state.skills.snapshot())
        tenant = next(Path(item["path"]) for item in runtime.plugin_paths
                      if Path(item["path"]).name == "ophira-tenant")
        assert (tenant / "skills" / skill["name"] / "SKILL.md").exists()
        assert not (tenant / "agents").exists()
        assert not (tenant / "hooks").exists()
        assert not (tenant / ".mcp.json").exists()
        refreshed = (await signed(client, "GET", detail_path)).json()
        assert {row["state"] for row in refreshed["skill_states"]} == {"pending", "published"}
    restarted = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=restarted), base_url="http://localhost",
    ) as client:
        recovered = (await signed(client, "GET", detail_path)).json()
        assert recovered["import"]["archive_hash"] == record["archive_hash"]
        assert {row["state"] for row in recovered["skill_states"]} == {"pending", "published"}


@pytest.mark.asyncio
async def test_plugin_import_rejects_archive_escapes_and_ambiguous_manifest(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        path = f"/api/plugins/imports?request_id={uuid4().hex}"
        escaped = plugin_zip(extra={"package/../outside.txt": "unsafe"})
        assert (await signed(client, "POST", path, content=escaped)).status_code == 422
        duplicate = plugin_zip(extra={
            "other/.claude-plugin/plugin.json": '{"name":"other"}',
        })
        assert (await signed(client, "POST", path, content=duplicate)).status_code == 422
        linked = io.BytesIO()
        with zipfile.ZipFile(linked, "w") as archive:
            archive.writestr("package/.claude-plugin/plugin.json", '{"name":"linked"}')
            info = zipfile.ZipInfo("package/skills/linked/SKILL.md")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        assert (await signed(client, "POST", path, content=linked.getvalue())).status_code == 422


def test_custom_skill_paths_and_inline_mcp_are_discovered_without_agent_loading():
    payload = plugin_zip(manifest={
        "name": "custom-plugin", "skills": ["./custom/skills"],
        "mcpServers": {"remote": {"type": "http", "url": "https://mcp.example.com/mcp"}},
        "agents": "./special/agents",
    }, extra={
        "package/.mcp.json": "{}",
        "package/custom/skills/custom/SKILL.md": (
            "---\nname: custom\ndescription: A custom skill\n---\nRead only.\n"
        ),
        "package/special/agents/helper.md": "Subagent instructions",
    })
    archive = inspect_plugin_archive(payload)
    assert {row.path for row in archive.skills} == {
        "skills/market-reader", "skills/price-check", "custom/skills/custom",
    }
    assert [row.url for row in archive.mcp_servers] == ["https://mcp.example.com/mcp"]
    assert "special/agents/helper.md" in archive.ignored_agents


def test_root_skill_and_manifest_mcp_path_do_not_load_plugin_runtime_components():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            ".claude-plugin/plugin.json",
            '{"name":"root-skill","mcpServers":"./.mcp.json"}',
        )
        archive.writestr(
            "SKILL.md", "---\nname: root-skill\ndescription: Root skill\n---\nRead.\n",
        )
        archive.writestr(
            ".mcp.json", '{"mcpServers":{"remote":{"type":"http",'
            '"url":"https://mcp.example.com/mcp"}}}',
        )
        archive.writestr("agents/worker.md", "Do work")
    result = inspect_plugin_archive(buffer.getvalue())
    assert len(result.skills) == 1
    assert set(result.skills[0].files) == {"SKILL.md"}
    assert [item.name for item in result.mcp_servers] == ["remote"]
    assert result.ignored_agents == ("agents/worker.md",)


def test_official_style_mcp_names_and_oauth_are_reported_without_importing_auth():
    payload = plugin_zip(extra={
        "package/.mcp.json": json.dumps({"mcpServers": {
            "research tools": {
                "type": "http", "url": "https://mcp.example.com/mcp",
                "oauth": {"clientId": "do-not-import"},
            },
            "google calendar": {"type": "http", "url": ""},
        }}),
    })
    result = inspect_plugin_archive(payload)
    assert [(item.name, item.credentials_required) for item in result.mcp_servers] == [
        ("research tools", True),
    ]
    assert "MCP server research tools: OAuth setup was not imported" in result.ignored_components
    assert any("google calendar" in item for item in result.ignored_components)
    assert "do-not-import" not in repr(result)


@pytest.mark.asyncio
async def test_invalid_later_skill_stages_no_earlier_skill(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        payload = plugin_zip(extra={
            "package/skills/price-check/SKILL.md": "missing frontmatter",
        })
        path = f"/api/plugins/imports?request_id={uuid4().hex}"
        invalid = await signed(client, "POST", path, content=payload)
        assert invalid.status_code == 422
        assert (await signed(client, "GET", "/api/skills/imports")).json() == []
        assert (await signed(client, "GET", "/api/plugins/imports")).json() == []


@pytest.mark.asyncio
async def test_plugin_list_cleans_only_safe_crash_staging(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    root = app.state.plugin_imports.root
    stage = root / ".plugin-inspect-abandoned"
    stage.mkdir()
    (stage / "candidate.txt").write_text("unpublished")
    temporary = root / f".plugin_import_{'a' * 64}.json.abandoned.tmp"
    temporary.write_text("uncommitted")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        result = await signed(client, "GET", "/api/plugins/imports")
        assert result.status_code == 200
        assert result.json() == []
    assert not stage.exists()
    assert not temporary.exists()


@pytest.mark.asyncio
async def test_local_operator_can_stage_plugin_without_activating_components(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        staged = await client.post(
            f"/api/plugins/imports?request_id={uuid4().hex}", content=plugin_zip(),
        )
        assert staged.status_code == 201, staged.text
        assert staged.json()["import"]["uploaded_by"]["kind"] == "local_owner"
        assert len(staged.json()["skill_states"]) == 2
        assert list((tmp_path / "user-mcp").rglob("*.json")) == []
