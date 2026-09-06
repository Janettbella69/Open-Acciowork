# 原生 SDK deferred tool 离线探针

2026-09-05 使用项目固定 `claude-agent-sdk==0.2.143` 及其实际 CLI 执行。`backend/tests/native_defer_probe.py` 仅绑定 `127.0.0.1`，返回人为编排的 Anthropic SSE 协议响应；使用 dummy API key、独立临时 `CLAUDE_CONFIG_DIR`、`setting_sources=[]`、严格 MCP 配置。没有真实模型调用、业务动作、用户文件或 secrets。Read 只读生成的临时文本；MCP fixture 只计数并返回固定文本。退出清理临时会话。

## 已测事实

| 探针 | 实际结果 |
| --- | --- |
| 单个 Read → SDK callback defer → 无新 query resume | 同 SDK session/tool ID 恢复，SDK 生成真实 Read 结果；不是通过伪造 tool_result 或新业务 prompt 继续 |
| 同一 API SSE message 内两个 tool_use | Python SDK 逐块产出 `AssistantMessage(tool0) → PreToolUse0 → AssistantMessage(tool1) → PreToolUse1`；第一条 AssistantMessage **不包含完整批次** |
| 直接对双工具返回 defer | 此 fixture 中最终只有第二个 tool ID 进入 deferred 结果；不能把它当成安全的多工具持久暂停接口 |
| hook 等待 StreamEvent.message_stop，单工具 | receive_response 能继续读取 message_stop；等待结束后证明 count=1，再 defer 成功 |
| 两个 hook 等待 message_stop，双工具 | 无死锁，两个 hook 都看见 count=2，返回 deny；SDK 为两工具生成错误结果，没有 Read 执行 |
| 恢复时仅用 SDK callback hook | 实测存在启动竞态：有的运行 callback 执行，有的运行完全没执行就已重放 Read；**不得作为恢复阶段唯一门禁** |
| 显式 options.settings startup command PreToolUse = defer | 恢复仍返回相同 tool ID 的 tool_deferred；无 Read 结果，也无第二次模型请求 |
| startup command = deny | 真实 SDK 生成拒绝 tool_result，不执行 Read；不是应用自己构造工具结果 |
| startup command = allow | 原 tool ID 的 Read 真正执行一次，然后 SDK 继续 |
| SDK in-process MCP fixture，startup allow/defer | 保持同一 MCP 配置后可恢复；allow 仅调用 fixture 一次，defer 不调用；未出现 tool_deferred_unavailable |

恢复 tool_result 可以出现在 SDK `SystemMessage(init)` 之前，因此 init 通知也不能当成“此前没有工具副作用”的证据。固定安装版本的 Python transport 在启动 CLI 时通过 `--settings` 传递显式设置，而 Python callback hooks 随后才由 SDK initialize 控制消息注册；这解释了早期 command hook 和 callback hook 的关键差别。

## 复现

```sh
.venv/bin/python backend/tests/native_defer_probe.py --count 1
.venv/bin/python backend/tests/native_defer_probe.py --count 2
.venv/bin/python backend/tests/native_defer_probe.py --count 1 --wait-batch
.venv/bin/python backend/tests/native_defer_probe.py --count 2 --wait-batch
.venv/bin/python backend/tests/native_defer_probe.py --count 1 --startup defer
.venv/bin/python backend/tests/native_defer_probe.py --count 1 --startup deny
.venv/bin/python backend/tests/native_defer_probe.py --count 1 --startup allow
.venv/bin/python backend/tests/native_defer_probe.py --tool-kind mcp --wait-batch --startup allow
.venv/bin/python backend/tests/native_defer_probe.py --tool-kind mcp --wait-batch --startup defer
```

脚本输出事件时间序列与末尾 SUMMARY，不保存 API request 正文或凭据日志。默认模式是需显式运行的原生边界探针，不在默认 pytest 集合中。新增 `--integrated` 模式通过生产 NativeHookBridge、RunManager、HTTP 审批接口和两套独立控制平面对象，测试真实 CLI 子进程暂停/恢复；其模型响应仍是本地 fixture。`--count 2` 不带 `--wait-batch` 是刻意复现错误假设的负例，只能在无副作用 fixture 环境使用。

## 集成必须保留的限制

应用在完整的主 assistant stream 边界证明恰好一个待执行工具，否则拒绝本次持久暂停。启动 command hook 是同一宿主授权策略的传输入口；桥不可用、凭据/绑定错误 fail closed。实现与剩余边界见 [durable-approvals.md](durable-approvals.md)。这些本地结果不等同于真实 Provider、手机或生产隔离验收。

## 2026-09-05 集成矩阵

`evals/reports/native-approval-20260905/` 归档四个通过的真实 SDK 离线集成报告：
MCP批准（只执行一次）、MCP拒绝（零执行）、Write（临时文件内容精确匹配）及
AskUserQuestion（原问题回填答案、同 Run 约束与 SDK 完成事件）。原请求均在第二控制平面可见，
重复审批返回409，恢复未发新query；每例仅两次本地fixture模型请求。

该矩阵发现并修复了 AskUserQuestion 回填问题：在 PreToolUse 修改 answers 后，SDK permission
callback 仍可收到原 questions 输入；现在先检查原输入，再在 callback 返回 updated_input。
回答的 SDK UserMessage/tool_result 在没有 PostToolUse 时仍产生去重审计，且不会将错误算成功。
单独的 spawn 双进程测试覆盖账本竞争；这里的两个控制平面对象不冒充两个独立服务进程。

官方说明将 defer 定义为保留单个待执行工具并在 resume 时再次触发 PreToolUse；批量工具不在其保证范围。原生测试用于核对固定安装版本的真实行为，不能用文档理想时序替代启动竞态验证。[Claude Code hooks：defer](https://code.claude.com/docs/en/hooks#defer-a-tool-call-for-later)
