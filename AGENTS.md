# TradeMind engineering rules

These rules apply to every contributor and AI agent in this repository (Claude Code reads them
through `CLAUDE.md`; Codex reads this file directly).

## 项目定位

TradeMind（代码名 OPHIRA）：跨境电商版的 Codex，面向出海新手，对标阿里的 Accio Work。一个
Claude Agent SDK Agent 从模糊的想法出发，在自己的工作区里替用户把跨境的活干完；需求、供给、
资金、销售是它按需读取的 Skill，不是流程或角色。

## 架构权威

- 唯一的架构文档是 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。改架构就在同一个提交里
  改它；不要再新增别的架构或 PRD 文档。
- **《Agent Harness 教材》是架构判断的最终权威（2026-08-24 用户拍板）。** 代码或文档与教材
  冲突，按教材改。下结论前先引原文和页码。2026-11-26 的逐条对照（反模式索引中 16 行）是
  这次重建的依据。

## 不可妥协的红线

- 一台电脑、一个循环、一个窗口：Agent 只经 `ClaudeSDKClient` 驱动；不手写工具循环、不写
  状态机、不设轮数上限、不按职责拆 Agent。禁止 OpenAI/Anthropic 裸客户端、LangGraph、
  LangChain、Dify。
- `tools` 与 `system_prompt` 用 `claude_code` preset；`setting_sources=[]`；Skill 只经本地
  插件 `backend/ophira-capabilities/` 加载；`strict_mcp_config=True`。
- **代码只待在教材 p.20 的四个位置**：Agent 启动之前的准备、基础设施（网关、计费、鉴权、
  多模型接入）、只管副作用的 Hook（备份、审计、拦截）、人把关之前的等待。代码不替 Agent
  判断下一步、不改写工具返回、不替 Agent 对用户说话。每段新代码先回答它属于哪个位置。
- 记忆就是文件：用 SDK 自带的 auto memory（每个项目自己的 `CLAUDE_CONFIG_DIR`），不自造
  记忆系统，不加审批流。
- 沙箱：`sandbox.enabled=True`、`allowUnsandboxedCommands=False`、`failIfUnavailable=True`、
  `autoAllowBashIfSandboxed=True`，不得关闭。
- Agent 的进程里不放模型密钥：模型调用只走 `gateway.py`，密钥在网关出口加上。
- 国产模型只走各厂商官方的 Anthropic 兼容端点，不做 OpenAI 协议转换；Coding Plan 类套餐的
  Key 禁止进入后端（厂商条款，见 `docs/model-providers.md`）。
- `claude-agent-sdk` 钉 `0.2.189`；升级前后都要跑 `OPHIRA_E2E_CLI=1` 的端到端测试和
  `deploy/shared-box/sandbox_acceptance.py`。

## 动手之前

1. 读本文件、`docs/ARCHITECTURE.md`、相关源码和测试、最近的 git 历史。
2. **和 Agent 有关的修复或新能力，先读 `docs/claude-agent-sdk/` 里对应的官方文档快照**
   （按它的 README 找），文档和实际行为冲突时以安装包源码为准：
   `.venv/lib/python3.11/site-packages/claude_agent_sdk/`。SDK 太新，凭记忆写必错。
3. 想加东西之前，先问「先拿到用户了吗」（教材 p.66）：没有真实用户在等的功能不做。连接器、
   渠道、多用户都按这条判断。
4. 拿不准时的参考顺序：官方文档与 SDK 源码 → 官方示例
   （github.com/anthropics/claude-agent-sdk-python 的 examples/）→ 本仓代码 → 先告诉用户
   你打算怎么做，等确认。

## 常用命令

```bash
# 后端（Python 3.11，仓库根目录）
python3.11 -m venv .venv && .venv/bin/pip install -e './backend[dev]'
cd backend
../.venv/bin/uvicorn ophira_backend.main:app --app-dir src --port 8000
PYTHONPATH=src ../.venv/bin/pytest
OPHIRA_E2E_CLI=1 PYTHONPATH=src ../.venv/bin/pytest tests/test_e2e_cli.py   # 真实 CLI，零花费
../.venv/bin/ruff check src tests

# 前端（React 19 + Vite 7）
cd frontend
npm run dev        # http://localhost:5173/app，/api 代理到 127.0.0.1:8000
npm run lint && npm run test:run && npm run build
```

## 验证与交付

- 改了行为就加测试；Agent 相关的改动要过真实 CLI 的端到端测试（脚本化本机模型，不花钱）。
- 界面改动在浏览器里真实走一遍（Playwright 在 `.venv` 里）。只跑单元测试不算端到端。
- 真实模型端点花钱：先报价、等用户点头再跑。
- 说「完成/修好/通过」之前，在对应目录实际跑测试和 lint，并给出输出。
- `backend/data/` 和 `backend/.env` 不提交；测试一律用 `tmp_path`。
- 提交小而聚焦；只暂存自己这次任务的文件。

## 沟通

用简体中文回复用户；代码、命令、路径、标识符保持原样。
