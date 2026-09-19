# OPHIRA Engineering Rules

These rules apply to every contributor and AI agent in this repository.

## 项目定位

OPHIRA：跨境电商版的 Codex，面向出海小 B，对标阿里的 Accio Work。实现上由一个 Claude Agent SDK 单主 Agent 从模糊商业目标出发，用原生工具 + Plugin Skills + 进程内 MCP 产出可追溯的判断、证据和交付物。

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

## AI 知识盲区提醒（先读文档，再写代码）

本项目的核心依赖 **Claude Agent SDK 是非常新的技术栈，AI 训练集里没有或已过时**。
**任何 AI 在准备开发与 Agent 有关的修复或新能力时，必须先检查
`docs/claude-agent-sdk/`（官方文档本地快照），找到相关官方文档、读完再开发——
不要凭记忆写。** 快照按场景的索引在该目录 README；文档与实际行为冲突时，以钉版
安装源码 `.venv/lib/python3.11/site-packages/claude_agent_sdk/` 为准。

## 参考优先级

遇到设计上拿不准的问题时，按以下顺序参考，不要自由发挥：

1. **官方**：`docs/claude-agent-sdk/`（官方文档快照，按 README 索引找）＋
   `.venv/lib/python3.11/site-packages/claude_agent_sdk/`（钉 0.2.143 的安装源码，
   行为的最终裁决）；快照不够新时上 https://code.claude.com/docs/en/agent-sdk/ 。
   这是官方的，逻辑完善，优先看这里。
2. **官方示例工程**：https://github.com/anthropics/claude-agent-sdk-python （examples/）与
   https://github.com/anthropics/claude-agent-sdk-demos ——工程质量比我们现在的代码成熟
   （在线参考，未本地化）。
3. **我们自己的现有代码**：`backend/src/ophira_backend/`（sdk_adapter.py 是 SDK 集成的
   唯一入口）——可能有历史包袱，参考但要判断。
4. 以上都没有答案时，**告诉用户你打算怎么做，等确认后再动手**。

## 常用命令

### 后端（FastAPI + Claude Agent SDK，Python 3.11）

```bash
# venv（注意：仓库 2026-08-20 由 Ophira-ai 更名而来，旧 venv shebang 会失效，需 --clear 重建）
python3.11 -m venv .venv --clear
.venv/bin/pip install -e './backend[dev]'

# 起服务（http://127.0.0.1:8000）
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload

# 测试 / 单个测试 / lint
PYTHONPATH=backend/src .venv/bin/pytest backend/tests
PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_sdk_adapter.py::test_default_sdk_options_keep_the_agent_general_and_observable -v
.venv/bin/ruff check backend/src backend/tests

# Green Path E2E（真端点，花真钱，需已配置并预检通过的 Provider profile）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_green_path.py --provider deepseek --rounds 3

# Provider 合约测试（真端点）
OPHIRA_CONTRACT_PROVIDER=deepseek PYTHONPATH=backend/src .venv/bin/pytest backend/tests/contract -v
```

### 前端（React 19 + Vite 7）

```bash
cd frontend
npm run dev        # http://localhost:5173/app；vite 代理 /api 与 SSE 到 127.0.0.1:8000
npm run test:run   # vitest 单跑
npx vitest run src/components/SettingsPage.test.tsx   # 单文件
npm run lint && npm run build
```

## 架构大图（跨文件才能看懂的部分）

一次运行的数据流：`POST /api/projects/{id}/runs`（main.py）→ `RunManager.create_and_start`（runs.py，固化 provider/skill 快照）→ `ClaudeAgentRuntime.run`（sdk_adapter.py，装配 `ClaudeAgentOptions` 并把 SDK 消息流映射为 27 种 SSE 事件）→ 事件经 `EventPublisher`（events.py，全量 `redact()` 脱敏）落 `FileLedger`（storage.py，JSON/JSONL 原子写）→ 前端 `useOpportunityRuntime.ts` 经 SSE（`Last-Event-ID` 续传）消费。

- **权限双层防御**：`can_use_tool` 回调（写入 workspace 包含检查 + 禁 unsandboxed Bash + `PermissionBroker` 实时授权）+ `PreToolUse` hook（敏感路径黑名单 + 拦逃逸沙箱；读写围栏分开：Read/Glob/Grep=项目目录〔含账本 project.json 直读〕，Write/Edit=工作区）。沙箱内 Bash 走 SDK 原生 `autoAllowBashIfSandboxed` 自动批准（v2.2 2026-08-24，教材口径；`failIfUnavailable=True` 沙箱起不来必须失败）；人把关只在账本写入/对外发送等业务动作的 MCP ask 层。前端权限卡片经 `POST /api/runs/{run_id}/permissions/{request_id}` 回传。
- **Provider 体系**（providers.py）：`ProviderRegistry` 文件存 profile（`backend/data/providers/*.json`，不含密钥），密钥在 `FileSecretVault`（`backend/data/secrets/`，0600）；`build_worker_env` 负责凭据隔离与全档位模型映射；`preflight()`（sdk_adapter.py）走真实 SDK `query()` 断言 Skill 发现 + MCP health 调用，错误分 8 类码。
- **MCP**（ledger_tools.py + web_tools.py + automation_tools.py）：6 个进程内 server 共 21 工具（ledger/supplier/customer/commerce_math/web/automation），工具描述一律声明"不代替商业判断"；v2.2 收敛：legacy 视图工具已删、market 域并入账本证据标签（教材"相同域合并"，用户拍板 MCP≈20）。
- **记忆**（memory.py）：Agent 只能提案，用户批准后生效；agent 作用域批准项经 marker 行镜像进该 Agent 的 `MEMORY.md`。
- **前端四页**（App.tsx 路由）：`/app` Workspace（WorkspaceComposer 为主体）、`/app/schedules`、`/app/skills`、`/app/settings`；`/` 是营销页。

## 给开发 Agent 的工作法（教材 CLAUDE.md 要点）

- 不做 MVP，不分阶段，不兼容老版本——直接把架构里的功能一次做完；任何地方不要出现 MVP/demo 字样（做的是真产品，只是还没上线）。
- 讨论方案的时候只读不写；拍板后再动代码。
- 犯错被纠正时，把教训写进记忆（用户级 auto-memory 或本仓文档），下次不再犯。

## 数据与验证纪律

- **PDF 与图片统一先转 Markdown 再进 Agent**（教材口径：PaddleOCR-VL；需要精确坐标溯源用 TextIn）。上传已接入有界 Markdown 摄取与上传原件 Read 门禁，VL 需显式配置本地模型；缺工具、空白页和截断必须如实标记，不得推断缺失内容。真实 VL 质量、生产解码隔离与 TextIn 坐标验收仍未完成，见 `docs/ocr-ingestion.md`，不能沿用历史审计的“完全未实现”描述或宣称已全量验收。
- 改完功能让 AI 做端到端验证：Web 面优先用浏览器真实走一遍（真实 query/真实文件），API 面跑 Green Path；只跑单测不算端到端。
- `backend/data/` 整体 gitignored（项目账本、runs、providers、secrets）；**绝不读取或提交 `backend/data/secrets/`**，测试一律用 `tmp_path`。
- 声称"完成/修好/通过"之前，先在对应目录实际跑测试/lint 并给出输出证据（见本文件 Verification and delivery 节）。
- 评测资产在 `evals/`（基准任务集、judge 模板、报告）；judge 未经 gold 校准不得进入发布门禁；门槛数值留空待基线填定（PRD §3.11）。
- 涉及真端点的测试（Green Path、合约）花真钱，默认被环境变量门控 skip，勿在 CI 或无意识场景触发。
