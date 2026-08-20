# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

OPHIRA：面向出海小 B 的 AI 全球商业系统，对标 Accio Work。一个 Claude Agent SDK 单主 Agent 从模糊商业目标出发，用原生工具 + Plugin Skills + 进程内 MCP 产出可追溯的判断、证据和交付物。

**权威规格**：`docs/OPHIRA-PRD-and-Technical-Architecture-v2.md`（PRD v2.1，产品需求+技术架构+验收门槛三合一）。
**工程规则**：`AGENTS.md` 对所有贡献者和 AI Agent 有约束力——改代码前先读它。

## 不可妥协的红线（详见 PRD §1.1、AGENTS.md）

- 核心 Agent 只能经 `ClaudeSDKClient`/`query()` 驱动；禁止 OpenAI/Anthropic 裸 Client、LangGraph/LangChain/Dify、手写工具循环。
- `tools` 与 `system_prompt` 用 `claude_code` preset；`setting_sources=[]`；应用 Skills 只经本地 Plugin（`backend/ophira-capabilities/`）装载；`strict_mcp_config=True`。
- 普通模式单主 Agent：`disallowed_tools=["Agent"]`、`agents=None`（有测试断言，"只读临时子代理"放宽提案待用户拍板，勿自行放开）；仅 Deep Research 模式注入 3 个只读研究子代理。
- `sandbox.enabled=True` 且 `allowUnsandboxedCommands=False` 不得关闭；worker env 必须先清空 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN` 再注入所选 Provider 的唯一凭据。
- 国产模型只走各厂商官方 Anthropic 兼容端点（Provider Profile），不做 OpenAI 协议转换；Coding Plan 套餐 Key 禁止进入后端（条款红线，见 PRD §4.9）。
- `claude-agent-sdk` 钉 `0.2.82`；升级必须先跑 Provider 合约测试。

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

- **权限双层防御**：`can_use_tool` 回调（workspace 路径包含检查 + 禁 unsandboxed Bash + `PermissionBroker` 实时授权，Bash 授权永不按 run 缓存）+ `PreToolUse` hook（敏感路径黑名单）。前端权限卡片经 `POST /api/runs/{run_id}/permissions/{request_id}` 回传。
- **Provider 体系**（providers.py）：`ProviderRegistry` 文件存 profile（`backend/data/providers/*.json`，不含密钥），密钥在 `FileSecretVault`（`backend/data/secrets/`，0600）；`build_worker_env` 负责凭据隔离与全档位模型映射；`preflight()`（sdk_adapter.py）走真实 SDK `query()` 断言 Skill 发现 + MCP health 调用，错误分 8 类码。
- **MCP**（ledger_tools.py）：5 个进程内 server 共 14 工具（ledger/market/supplier/customer/commerce_math），工具描述一律声明"不代替商业判断"。
- **记忆**（memory.py）：Agent 只能提案，用户批准后生效；agent 作用域批准项经 marker 行镜像进该 Agent 的 `MEMORY.md`。
- **前端四页**（App.tsx 路由）：`/app` Workspace（WorkspaceComposer 为主体）、`/app/schedules`、`/app/skills`、`/app/settings`；`/` 是营销页。

## 数据与验证纪律

- `backend/data/` 整体 gitignored（项目账本、runs、providers、secrets）；**绝不读取或提交 `backend/data/secrets/`**，测试一律用 `tmp_path`。
- 声称"完成/修好/通过"之前，先在对应目录实际跑测试/lint 并给出输出证据（AGENTS.md §Verification）。
- 评测资产在 `evals/`（基准任务集、judge 模板、报告）；judge 未经 gold 校准不得进入发布门禁；门槛数值留空待基线填定（PRD §3.11）。
- 涉及真端点的测试（Green Path、合约）花真钱，默认被环境变量门控 skip，勿在 CI 或无意识场景触发。
