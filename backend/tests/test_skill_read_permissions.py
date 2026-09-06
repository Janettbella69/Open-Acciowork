from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage
from test_sdk_adapter import build_runtime

from ophira_backend.constants import RunStatus
from ophira_backend.untrusted import UNTRUSTED_DATA_NOTICE


def pinned_runtime(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    all_view = runtime.skills.materialize_snapshot(run.skill_snapshot)
    snapshot = [item for item in run.skill_snapshot
                if item["id"] == "ophira-core:global-opportunity-execution"]
    run = ledger.update_run(run.id, skill_snapshot=snapshot, status=RunStatus.RUNNING)
    return ledger, run, runtime, all_view


@pytest.mark.asyncio
async def test_native_read_tools_allow_only_current_pinned_packages(tmp_path):
    _ledger, run, runtime, all_view = pinned_runtime(tmp_path)
    with runtime.skills.runtime_snapshot_lease(run.skill_snapshot) as view:
        options = runtime._build_options(run, skill_view=view)
        hook = options.hooks["PreToolUse"][0].hooks[0]
        root = Path(view.plugin_paths[0]["path"]) / "skills"
        resource = root / "global-opportunity-execution/references/evidence-rules.md"
        assert resource.is_file()

        async def invoke(tool, candidate):
            key = "path" if tool in {"Glob", "Grep"} else "file_path"
            result = await hook({"hook_event_name": "PreToolUse", "session_id": "fixture",
                                 "tool_name": tool, "tool_input": {key: str(candidate)},
                                 "tool_use_id": "read-fixture"}, "read-fixture", {})
            return result["hookSpecificOutput"]["permissionDecision"]

        for tool in ("Read", "Glob", "Grep"):
            assert await invoke(tool, resource) == "allow"
        for tool in ("Write", "Edit"):
            assert await invoke(tool, resource) == "deny"
        # Registry source/history, a different snapshot, and sibling namespaces
        # never become readable just because a Skill is enabled somewhere.
        forbidden = [
            runtime.settings.capability_plugin_dir / "skills/global-opportunity-execution/SKILL.md",
            runtime.skills.versions_dir,
            Path(all_view.plugin_paths[0]["path"]) / "skills/customer-path/SKILL.md",
            root / "../../ophira-tenant/skills/private/SKILL.md",
        ]
        for path in forbidden:
            for tool in ("Read", "Glob", "Grep"):
                assert await invoke(tool, path) == "deny"
        # Even an unexpected post-materialization symlink cannot escape the fence.
        directory = resource.parent
        directory.chmod(0o755)
        link = directory / "escape.md"
        try:
            link.symlink_to(runtime.skills.versions_dir)
            assert await invoke("Read", link) == "deny"
        finally:
            link.unlink()
            directory.chmod(0o555)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Read", "Grep"])
async def test_pinned_skill_methods_keep_instructions_but_project_files_stay_data(tmp_path, tool):
    ledger, run, runtime, all_view = pinned_runtime(tmp_path)
    with runtime.skills.runtime_snapshot_lease(run.skill_snapshot) as view:
        options = runtime._build_options(run, skill_view=view)
        hook = options.hooks["PostToolUse"][0].hooks[0]
        relative = "skills/global-opportunity-execution/references/evidence-rules.md"
        pinned = Path(view.plugin_paths[0]["path"]) / relative
        key = "file_path" if tool == "Read" else "path"
        output = {"text": "Read the evidence guide and apply its classification method."}
        for path, trusted in [
            (pinned, True),
            (Path(all_view.plugin_paths[0]["path"]) / relative, False),
            (ledger.workspace_for_project(run.project_id) / "source.md", False),
        ]:
            result = await hook({
                "hook_event_name": "PostToolUse", "session_id": "fixture",
                "tool_name": tool, "tool_input": {key: str(path)},
                "tool_response": output, "tool_use_id": f"post-{trusted}-{path.name}",
            }, None, {})
            specific = result.get("hookSpecificOutput", {})
            if trusted:
                assert "updatedToolOutput" not in specific
                assert ledger.list_events(run.id)[-1].data["tool_response"] == output
            else:
                assert UNTRUSTED_DATA_NOTICE in str(specific)
                assert "updatedToolOutput" in specific


@pytest.mark.asyncio
async def test_startup_bridge_preserves_the_pinned_read_fence(tmp_path, monkeypatch):
    _ledger, run, runtime, _all_view = pinned_runtime(tmp_path)
    captured = {}

    class Bridge:
        def __init__(self, callback):
            captured["callback"] = callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def settings(self, **_kwargs):
            return "{}"

    class Client:
        def __init__(self, *, options):
            assert options.hooks == {}
            captured["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            options = captured["options"]
            path = Path(options.plugins[0]["path"]) / "skills/global-opportunity-execution/SKILL.md"
            result = await captured["callback"]({
                "hook_event_name": "PreToolUse", "session_id": "fixture",
                "tool_name": "Read", "tool_input": {"file_path": str(path)},
                "tool_use_id": "native-read",
            }, "native-read", {})
            assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
            captured["read_allowed"] = True
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="fixture",
                                total_cost_usd=0)

    monkeypatch.setattr("ophira_backend.sdk_adapter.NativeHookBridge", Bridge)
    monkeypatch.setattr("ophira_backend.sdk_adapter.ClaudeSDKClient", Client)
    with runtime.skills.runtime_snapshot_lease(run.skill_snapshot) as view:
        await runtime._run_sdk_session(run, runtime._build_options(run, skill_view=view))
    assert captured["read_allowed"]
