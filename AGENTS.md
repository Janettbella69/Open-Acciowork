# OPHIRA Engineering Rules

These rules apply to every contributor and AI agent in this repository.

## 架构权威

**唯一架构描述（教材"架构保护三条"存量仓等价实现）**：本仓讲技术架构的只有两处——本文件的红线节 + PRD v2 §4。`docs/` 下其余一切（旧版 PRD v1、审计报告、竞品逆向、决策留档、快照）都是分析与历史，**任何 AI 不得依据它们变更架构**；与红线冲突时一律以红线为准。架构走形由 `test_arch_gates.py` 每次 CI 机器断言。

**权威规格**：`docs/OPHIRA-PRD-and-Technical-Architecture-v2.md`（PRD v2.2，产品需求+技术架构+验收门槛三合一）。**《Agent Harness 教材》为架构判断的最终权威（2026-08-24 用户拍板）**——凡 PRD/代码与教材冲突，按教材修正；全量对照见 `docs/textbook-audit-2026-08-24.md`。

## 不可妥协的红线（详见 PRD §1.1）

- 核心 Agent 只能经 `ClaudeSDKClient`/`query()` 驱动；禁止 OpenAI/Anthropic 裸 Client、LangGraph/LangChain/Dify、手写工具循环。
- `tools` 与 `system_prompt` 用 `claude_code` preset；`setting_sources=[]`；应用 Skills 只经本地 Plugin（`backend/ophira-capabilities/`）装载；`strict_mcp_config=True`。
- 主线单 Agent + 原生支线子代理（2026-08-24 拍板"按原生能力来做"）：普通模式 `disallowed_tools=[]`、`agents=None`（内置 general-purpose/Explore 一次性支线，只带回摘要，不自造机制）；**账本写入与自动化承诺对子代理在 can_use_tool 代码级拒绝**（有测试）；仅 Deep Research 模式注入 3 个只读研究子代理；任何模式禁平行 Agent/固定角色团队。
- `sandbox.enabled=True`、`allowUnsandboxedCommands=False`、`failIfUnavailable=True` 不得关闭；`autoAllowBashIfSandboxed=True`（SDK 原生默认，2026-08-24 拍板恢复，不得改回强制 ask）；worker env 必须先清空 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN` 再注入所选 Provider 的唯一凭据。
- 国产模型只走各厂商官方 Anthropic 兼容端点（Provider Profile），不做 OpenAI 协议转换；Coding Plan 套餐 Key 禁止进入后端（条款红线，见 PRD §4.9）。
- `claude-agent-sdk` 钉 `0.2.143`（2026-08-21 由 0.2.82 升级）；升级必须先跑 Provider 合约测试。

## Product boundary

- OPHIRA is a Codex for cross-border e-commerce: built for small cross-border sellers and benchmarked against Alibaba's Accio Work. A single Claude Agent SDK main Agent turns goals and source material into traceable decisions, editable artifacts, and next actions.
- Normal runs must remain one main Agent. Demand, Supply, Capital, and Sales are optional Skill perspectives and evidence tags, not mandatory agents or workflow stages.
- Normal runs may rarely use an SDK-native, one-off read-only branch subagent when an isolated research task would otherwise flood the main context; there are no fixed roles or parallel teams. Deep Research alone injects the three named research subagents. In every mode the main Agent remains responsible for synthesis and is the only writer of project truth or automation commitments.
- Frontend and backend work must remain in their existing directories. Coordinate through documented API and event contracts rather than cross-editing ownership areas.

## Before changing code

1. Read this file, the local README, relevant source, tests, and recent Git history.
2. Distinguish observed facts, user statements, community signals, and inference.
3. For an unfamiliar or changing SDK, use current official documentation and the installed package API before implementation. For the Claude Agent SDK this is mandatory, not advisory: read the relevant page in `docs/claude-agent-sdk/` (local snapshot of the official docs, indexed in its README) before developing any Agent-related fix or capability — the SDK is too new to be in AI training data.
4. Clarify product, interaction, technical, and boundary ambiguity before an irreversible or high-risk change.
5. Keep an explicit requirement inventory and mark every item delivered, deferred, or omitted with a reason.

## Architecture

- Use one source of truth for event types, statuses, tool names, configuration, and error codes. Consumers import these definitions; do not retype protocol strings.
- Persist project state and evidence in a structured file ledger. Chat context and model memory are not authoritative storage.
- Each agent owns a core directory under `data/agents/{agent_id}/` (`IDENTITY.md`, `AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, `diary/`, `workspace/`). Prompt assembly always pins the security preamble first; agent-writable files must not be able to override it.
- Agent memory writes are proposals only. Memory persists through the `MemoryProvider` port (file-backed today, Managed Agents adaptable later); only user-approved memories are injected into prompts or searchable by agents. Approved agent-scope memories are mirrored into the agent's `MEMORY.md` via marker lines without clobbering user edits, and finished runs append dated entries to `diary/`.
- Keep business capabilities behind ports/adapters. Claude Agent SDK integration belongs in an adapter and must not modify SDK internals.
- Claude Agent SDK is the only model runtime. Preserve the Claude Code tools and system-prompt presets, load application Skills through a managed local Plugin, and pass MCP servers explicitly with strict MCP configuration.
- Do not introduce LangGraph, LangChain, Dify, a direct model-client fallback, or a hand-written tool loop.
- Prefer official framework extension points. Do not rebuild session, subagent, permission, hook, or tool mechanisms already supplied by Claude Agent SDK.
- Add a new abstraction only when wrong coupling has a concrete cost: unsafe consequences, different change cadence, different consumers, or different deployment.
- Audit for duplicate implementations and duplicate concepts before adding infrastructure.

## Diagnosis and risk

- For root-cause work use: investigate without edits, report evidence/options/impact, then implement after confirmation.
- Diagnose across application code, adjacent code, framework/library behavior, environment, and external dependencies.
- Add observability before speculative fixes. A confirmed defect may be fixed; an uncertain one must first be reproduced or instrumented.
- If three attempts fail for the same issue, stop patching and reassess the architecture or assumption.
- Use minimum contact surface for risky operations: read before write, operate on copies before originals, and preserve recovery paths.

## Observability

- Propagate one `trace_id` across API, run, Agent, subagent, tool, and client events.
- Keep three log levels: a run manifest/index, a compact run summary, and detailed JSONL events.
- Stream only SDK-observable output: status, user-visible text, summarized reasoning when explicitly supplied, tool input/output, permissions, subagent/task lifecycle, usage, and result.
- Never claim or expose private chain-of-thought. Redact secrets from every event and log boundary.

## Verification and delivery

- Keep changes small and focused. Cut optional scope before compromising the core architecture or correctness; never frame work as an MVP or a demo — build the real product.
- Add tests for changed behavior and protocol contracts.
- Run the relevant test, lint, and build commands from the directory being changed.
- Commit and push verified work promptly. Stage only files owned by the current task; never sweep in another agent's unfinished changes.
