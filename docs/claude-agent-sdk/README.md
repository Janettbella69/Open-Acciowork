# Claude Agent SDK 官方文档快照（本地）

> **给所有 AI / 贡献者的硬规则：开发任何与 Agent 有关的修复或新能力之前，
> 必须先在本文件夹找到相关官方文档读完再动手。**
> Claude Agent SDK 是非常新的技术栈，AI 训练集里没有或已过时——凭记忆写必错。

- 快照来源：https://code.claude.com/docs/en/agent-sdk/ （官方，`<slug>.md` 可取原始 markdown）
- 快照日期：**2026-08-20**（每篇开头的 Documentation Index 引言是源站自带的，非本仓添加）
- 版本口径：线上文档描述**最新版 SDK**；本项目钉 `claude-agent-sdk==0.2.82`。
  文档与实际行为冲突时，以安装包源码为准：
  `.venv/lib/python3.11/site-packages/claude_agent_sdk/`（升级须先过 Provider 合约测试，见根 CLAUDE.md 红线）。

## 快速索引（按本项目使用场景）

| 要做的事 | 先读 |
|---|---|
| 任何 SDK 相关改动的第一站 | `python.md`（Python API 全参考：query/ClaudeSDKClient/ClaudeAgentOptions/全部类型） |
| 权限（can_use_tool / 模式 / 规则） | `permissions.md` + `user-input.md`（审批回传） |
| Hook（PreToolUse 等生命周期拦截） | `hooks.md` |
| 进程内 MCP 工具（我们的 7 个 server） | `custom-tools.md`；外部/传输型 MCP 看 `mcp.md`；工具太多看 `tool-search.md` |
| Skills / 本地 Plugin 装载 | `skills.md` + `plugins.md` + `claude-code-features.md`（setting_sources） |
| 系统提示 preset / append | `modifying-system-prompts.md` |
| 会话续传 / 恢复 / fork | `sessions.md`；外置转录（Phase 4 云 Runner）看 `session-storage.md` |
| 子代理（仅 Deep Research 模式） | `subagents.md` |
| 流式输入/输出 | `streaming-vs-single-mode.md` / `streaming-output.md` |
| 结构化输出 | `structured-outputs.md` |
| 成本与用量 | `cost-tracking.md` |
| 沙箱 / 凭据隔离 / 生产部署 | `secure-deployment.md` + `hosting.md` |
| 报错查因 | `troubleshooting.md`；机制原理看 `agent-loop.md`；产品定位看 `overview.md` |

## 刷新快照

```bash
cd docs/claude-agent-sdk && for p in overview agent-loop python permissions hooks \
  custom-tools mcp tool-search sessions session-storage subagents skills plugins \
  modifying-system-prompts claude-code-features streaming-vs-single-mode \
  streaming-output user-input structured-outputs cost-tracking secure-deployment \
  hosting troubleshooting; do \
  curl -sL "https://code.claude.com/docs/en/agent-sdk/$p.md" -o "$p.md"; done
```

刷新后更新本 README 的快照日期；全目录见 https://code.claude.com/docs/llms.txt 。
