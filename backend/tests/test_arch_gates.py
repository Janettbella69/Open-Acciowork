"""Architecture static gates (PRD §6.1). These run in CI as L1 deterministic checks.

Any failure here means the change violates a non-negotiable red line, not a style rule.
"""

import ast
import re
from pathlib import Path

from ophira_backend.config import Settings
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src" / "ophira_backend"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Bare model clients and orchestration frameworks are banned as the core Agent loop.
FORBIDDEN_TOP_LEVEL_IMPORTS = {"openai", "anthropic", "langchain", "langgraph", "dify"}


def iter_imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_business_code_never_imports_a_replacement_agent_loop() -> None:
    offenders: list[str] = []
    for path in sorted(BACKEND_SRC.glob("*.py")):
        hits = iter_imported_top_level_modules(path) & FORBIDDEN_TOP_LEVEL_IMPORTS
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    assert not offenders, f"Forbidden model/orchestration imports found: {offenders}"


def test_claude_agent_sdk_version_is_pinned_exactly() -> None:
    content = PYPROJECT.read_text(encoding="utf-8")
    assert re.search(r'"claude-agent-sdk==\d+\.\d+\.\d+"', content), (
        "claude-agent-sdk must stay exactly pinned; upgrades go through the contract suite"
    )


def test_runtime_options_keep_the_governance_envelope(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Gate", objective="Check"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(settings, ledger, events, permissions)

    options = runtime._build_options(run)

    assert options.sandbox == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
    }
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    # Standard mode: one main Agent, no delegation, no role teams.
    assert options.disallowed_tools == ["Agent"]
    assert options.agents is None
    # Credential blanking happens before profile credential injection.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name in options.env


async def test_event_log_never_contains_credential_canaries(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Canary", objective="Check"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)

    from ophira_backend.constants import EventType

    await events.publish(
        run.id,
        EventType.SDK_SYSTEM,
        {
            "note": "Bearer sk-canary-token-abc123",
            "api_key": "sk-ant-canary999",
            "nested": {"password": "hunter2", "detail": "token=canary-tok"},
        },
    )

    raw = (tmp_path / "runs" / run.id / "events.jsonl").read_text(encoding="utf-8")
    for canary in ("sk-canary-token-abc123", "sk-ant-canary999", "hunter2", "canary-tok"):
        assert canary not in raw
    assert "[REDACTED]" in raw
