"""One computer, one loop, one window (textbook p.3).

The Claude Agent SDK runs the loop; this module prepares a turn and gets out of the
way. Host code appears only in the four places the textbook allows (p.20):

- before the Agent starts: the workspace snapshot, the system prompt, the options;
- infrastructure: the model gateway and the per-run cost fuse;
- a PreToolUse hook that only intercepts: deletes go to the person, writes outside
  the project are refused;
- the person's decision: approvals and AskUserQuestion wait for a human answer.

Nothing here decides what the Agent should do next, rewrites what a tool returned,
or speaks to the user on the Agent's behalf.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
)

from .destructive import is_destructive_delete
from .gateway import GATEWAY_PATH, ModelGateway
from .settings import Settings
from .store import Store, new_id, now

logger = logging.getLogger(__name__)

EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
# Reading, looking things up and delegating have no side effects on the person's world.
AUTO_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent"]
MODEL_TIER_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# Appended to the claude_code preset. 你是谁 / 你有什么 / 注意事项 (textbook p.94);
# the background block comes first because 背景不可替代 (p.45).
SYSTEM_PROMPT_APPEND = """# 业务背景

我们是 TradeMind，服务出海小 B（跨境卖家与小型贸易团队）：用户说出想法，你把活干完。
用户多是刚起步的出海新手，不少外贸术语和流程并不熟悉。
用户拿你的产出做真金白银的决策（选品、下单、报价、谈判），所以交付水准是：
每个关键结论可回链到证据、数字有口径、不确定就说不确定——宁可少而实，不可多而虚。

# 你是谁

你是 TradeMind。从用户的一个想法出发，替他把跨境生意的活干完：市场调研、选品、找供应商、算落地成本、写询盘和上架文案、规划开店和运营。

# 你有什么

- 当前目录就是这个项目的工作区：资料、草稿、表格和交付物都放在这里，用户上传的文件在 uploads/。
- 算数、比价、做表、整理数据，自己写 Python 跑。
- 需要外面的信息，就上网查。
- 跨境做事的经验在 Skill 里，用得上就读。

# 注意

- 什么时候问人：动作不可逆时问；信息缺失时自己找；判断有分歧时用 AskUserQuestion 给 2–4 个选项让用户选，别开放式追问，也别自己脑补。
- 不伪造数据、来源、工具结果或完成状态。
"""

ClientFactory = Callable[[ClaudeAgentOptions], Any]


class RunBusyError(RuntimeError):
    pass


def to_jsonable(value: Any) -> Any:
    """SDK messages as they are: class name plus fields, nothing re-typed (p.96)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {"type": type(value).__name__}
        for field in dataclasses.fields(value):
            if field.name != "signature":  # opaque thinking signature, not readable content
                out[field.name] = to_jsonable(getattr(value, field.name))
        return out
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class LiveRun:
    """A turn in progress: its event log in memory for live readers, and open questions."""

    def __init__(self, store: Store, run: dict[str, Any]) -> None:
        self.store = store
        self.run = run
        self.events: list[dict[str, Any]] = []
        self.changed = asyncio.Condition()
        self.pending: dict[str, tuple[asyncio.Future, dict[str, Any]]] = {}
        self.client: Any = None
        self.cancel_requested = False
        self.done = False

    async def emit(self, source: str, type_: str, data: Any) -> None:
        event = {"seq": len(self.events) + 1, "at": now(), "source": source,
                 "type": type_, "data": data}
        self.store.append_event(self.run["project_id"], self.run["id"],
                                json.dumps(event, ensure_ascii=False, default=str))
        async with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    async def ask_person(self, tool_name: str, tool_input: dict[str, Any],
                         tool_use_id: str | None) -> dict[str, Any]:
        request_id = new_id("req")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        request = {"request_id": request_id, "tool_name": tool_name,
                   "tool_input": tool_input, "tool_use_id": tool_use_id}
        self.pending[request_id] = (future, request)
        await self.emit("host", "permission.requested", request)
        try:
            decision = await future
        finally:
            self.pending.pop(request_id, None)
        await self.emit("host", "permission.resolved", {"request_id": request_id, **decision})
        return decision

    def answer(self, request_id: str, decision: dict[str, Any]) -> bool:
        entry = self.pending.get(request_id)
        if entry is None or entry[0].done():
            return False
        entry[0].set_result(decision)
        return True

    def close_pending(self, reason: str) -> None:
        for future, _ in list(self.pending.values()):
            if not future.done():
                future.set_result({"allow": False, "message": reason})


class AgentRunner:
    def __init__(self, settings: Settings, store: Store, gateway: ModelGateway,
                 gateway_url: str, client_factory: ClientFactory = ClaudeSDKClient) -> None:
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.gateway_url = gateway_url.rstrip("/")
        self.client_factory = client_factory
        self.live: dict[str, LiveRun] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- what the Agent gets before it starts -------------------------------

    def model_env(self, project_id: str) -> dict[str, str]:
        env = {
            # Clear anything the host process might carry; the only credential the CLI
            # sees is the gateway token.
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "ANTHROPIC_AUTH_TOKEN": self.gateway.token,
            "ANTHROPIC_BASE_URL": f"{self.gateway_url}{GATEWAY_PATH}",
            "CLAUDE_CONFIG_DIR": str(self.store.config_dir(project_id)),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # The computer's `python` is this backend's, which has the Office libraries
            # the office-documents Skill relies on.
            "PATH": os.pathsep.join([str(Path(sys.executable).parent),
                                     os.environ.get("PATH", "")]),
        }
        if self.settings.model:
            # Third-party endpoints only know their own model names, so every tier the
            # CLI may ask for (subagents, summaries) maps to the configured one.
            env.update({name: self.settings.model for name in MODEL_TIER_VARS})
        if self.settings.model_context_window:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.settings.model_context_window)
        return env

    def writable(self, project_id: str, candidate: str | None) -> bool:
        """The Agent writes in its workspace and in its own memory folder, nowhere else."""
        if not candidate:
            return False
        workspace = self.store.workspace(project_id).resolve()
        path = Path(candidate)
        target = (path if path.is_absolute() else workspace / path).resolve()
        if target.is_relative_to(workspace):
            return True
        projects = (self.store.config_dir(project_id) / "projects").resolve()
        if target.is_relative_to(projects):
            parts = target.relative_to(projects).parts
            return len(parts) >= 3 and parts[1] == "memory"
        return False

    def options(self, live: LiveRun, resume: str | None) -> ClaudeAgentOptions:
        project_id = live.run["project_id"]

        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None,
                               context: Any) -> dict[str, Any]:
            name = input_data.get("tool_name", "")
            args = input_data.get("tool_input") or {}
            if name in EDIT_TOOLS and not self.writable(
                project_id, args.get("file_path") or args.get("notebook_path")
            ):
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "只能写本项目工作区（和你的记忆目录）里的文件。",
                }}
            if is_destructive_delete(name, args):
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "ask",
                    "permissionDecisionReason": "删除文件要用户确认。",
                }}
            return {}

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any):
            # Edits reaching here already passed the boundary hook; the snapshot taken
            # before this turn is their backup, so they need no click.
            if tool_name in EDIT_TOOLS:
                return PermissionResultAllow(updated_input=tool_input)
            decision = await live.ask_person(tool_name, tool_input,
                                             getattr(context, "tool_use_id", None))
            if not decision.get("allow"):
                return PermissionResultDeny(message=decision.get("message") or "用户拒绝了。")
            if tool_name == "AskUserQuestion":
                updated = {"questions": tool_input.get("questions", [])}
                if decision.get("answers"):
                    updated["answers"] = decision["answers"]
                if decision.get("response"):
                    updated["response"] = decision["response"]
                return PermissionResultAllow(updated_input=updated)
            return PermissionResultAllow(updated_input=tool_input)

        return ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_PROMPT_APPEND},
            cwd=str(self.store.workspace(project_id)),
            env=self.model_env(project_id),
            setting_sources=[],
            plugins=[{"type": "local", "path": str(self.settings.capabilities_dir)}],
            skills="all",
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="default",
            allowed_tools=AUTO_ALLOWED_TOOLS,
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]},
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
            },
            thinking={"type": "adaptive", "display": "summarized"},
            max_budget_usd=self.settings.max_budget_usd,
            resume=resume,
        )

    # -- a turn -------------------------------------------------------------

    async def start(self, project_id: str, prompt: str) -> dict[str, Any]:
        if any(r.run["project_id"] == project_id for r in self.live.values()):
            raise RunBusyError(project_id)
        project = self.store.get_project(project_id)
        run = self.store.create_run(project_id, prompt)
        live = LiveRun(self.store, run)
        self.live[run["id"]] = live
        task = asyncio.create_task(self._run(live, project.get("session_id")))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _run(self, live: LiveRun, resume: str | None) -> None:
        project_id, run_id = live.run["project_id"], live.run["id"]
        status, error = "completed", None
        try:
            await asyncio.to_thread(self.store.snapshot, project_id, f"before {run_id}")
            await live.emit("host", "run.started", {"prompt": live.run["prompt"]})
            async with self.client_factory(self.options(live, resume)) as client:
                live.client = client
                await client.query(live.run["prompt"])
                async for message in client.receive_response():
                    await live.emit("sdk", type(message).__name__, to_jsonable(message))
                    if isinstance(message, ResultMessage):
                        self.store.update_project(project_id, session_id=message.session_id)
                        if message.is_error:
                            status, error = "failed", message.result or message.subtype
        except Exception as exc:  # the turn failed; the log says how, the server stays up
            logger.exception("run %s failed", run_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if live.cancel_requested:
                status, error = "cancelled", None
            live.close_pending("这一轮已经结束。")
            try:
                await asyncio.to_thread(self.store.snapshot, project_id, f"after {run_id}")
            except Exception:
                logger.exception("snapshot after %s failed", run_id)
            self.store.finish_run(project_id, run_id, status, error)
            await live.emit("host", "run.finished", {"status": status, "error": error})
            async with live.changed:
                live.done = True
                live.changed.notify_all()
            self.live.pop(run_id, None)

    async def cancel(self, run_id: str) -> bool:
        live = self.live.get(run_id)
        if live is None:
            return False
        live.cancel_requested = True
        live.close_pending("用户停止了这一轮。")
        if live.client is not None:
            await live.client.interrupt()
        return True

    def answer(self, run_id: str, request_id: str, decision: dict[str, Any]) -> bool:
        live = self.live.get(run_id)
        return live is not None and live.answer(request_id, decision)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
