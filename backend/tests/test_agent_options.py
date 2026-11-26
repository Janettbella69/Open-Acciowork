"""What the Agent is given before it starts (textbook p.3, p.20)."""

from __future__ import annotations

from pathlib import Path

from ophira_backend.agent import AUTO_ALLOWED_TOOLS, SYSTEM_PROMPT_APPEND, LiveRun


async def options_for(harness, project_id: str, resume: str | None = None):
    run = harness.store.create_run(project_id, "x")
    return harness.runner.options(LiveRun(harness.store, run), resume)


async def test_one_computer_one_loop_no_custom_tools(harness):
    project = await harness.project()
    options = await options_for(harness, project["id"])
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.system_prompt["preset"] == "claude_code"
    assert options.mcp_servers == {} and options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.max_turns is None  # no turn limit (p.15)
    assert options.agents is None
    assert options.cwd == str(harness.store.workspace(project["id"]))
    assert options.skills == "all"
    assert options.plugins == [{"type": "local", "path": str(harness.settings.capabilities_dir)}]
    assert options.max_budget_usd == harness.settings.max_budget_usd


async def test_bash_stays_on_inside_the_sdk_sandbox(harness):
    project = await harness.project()
    options = await options_for(harness, project["id"])
    assert "Bash" not in (options.disallowed_tools or [])
    assert options.sandbox == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
    }
    assert set(AUTO_ALLOWED_TOOLS) == {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent"}


async def test_sdk_memory_is_on_and_lives_in_the_project(harness, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "sonnet")  # host leftovers must not leak
    project = await harness.project()
    env = (await options_for(harness, project["id"])).env
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(harness.store.config_dir(project["id"]))
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "fake-model"
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "fake-model"


async def test_the_agent_never_holds_the_model_key(harness):
    harness.settings.model_auth_token = type(harness.settings.model_auth_token)("sk-real")
    project = await harness.project()
    env = (await options_for(harness, project["id"])).env
    assert "sk-real" not in env.values()
    assert env["ANTHROPIC_AUTH_TOKEN"] == harness.app.state.gateway.token
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000/_model"
    assert env["ANTHROPIC_API_KEY"] == env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


async def test_resume_continues_the_same_conversation(harness):
    project = await harness.project()
    assert (await options_for(harness, project["id"], resume="sess-9")).resume == "sess-9"


def test_system_prompt_carries_the_business_background_on_every_path():
    assert SYSTEM_PROMPT_APPEND.startswith("# 业务背景")
    assert "出海新手" in SYSTEM_PROMPT_APPEND
    for heading in ("# 你是谁", "# 你有什么", "# 注意"):
        assert heading in SYSTEM_PROMPT_APPEND


async def test_write_boundary(harness):
    project = await harness.project()
    pid = project["id"]
    workspace = harness.store.workspace(pid)
    memory = harness.store.config_dir(pid) / "projects" / "-some-key" / "memory" / "MEMORY.md"
    writable = harness.runner.writable
    assert writable(pid, str(workspace / "notes" / "plan.md"))
    assert writable(pid, "relative/inside.md")
    assert writable(pid, str(memory))
    assert not writable(pid, str(harness.store.config_dir(pid) / "settings.json"))
    assert not writable(pid, str(Path(workspace).parent / "project.json"))
    assert not writable(pid, "/etc/passwd")
    assert not writable(pid, str(workspace / ".." / "runs" / "x.json"))
    assert not writable(pid, None)


async def test_a_symlink_out_of_the_workspace_is_not_a_way_out(harness, tmp_path):
    project = await harness.project()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = harness.store.workspace(project["id"]) / "link"
    link.symlink_to(outside)
    assert not harness.runner.writable(project["id"], str(link / "file.txt"))
