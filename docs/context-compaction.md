# 会话上下文占用与压缩（FR-34）

当前窗口占用仅来自钉版 Claude Agent SDK `ClaudeSDKClient.get_context_usage()`。
Run 的 `context.usage` 事件把 `totalTokens`、有效窗口 `maxTokens` 分别保存为
`used_tokens`、`max_tokens`，并保存模型、采样时的压缩 epoch 和来源；无已测量读数时
界面显示未知，不用累计 `ResultMessage.usage`
或 `model_usage` 推算窗口占用。运行中每 5 秒尝试采样一次，仅在读数变化时追加事件；
新连接和每个 SDK Result 后也采样。工作台的数值注明上次测量时间，刷新后从 Run
事件流恢复。

SDK 的 `SystemMessage(subtype="compact_boundary")` 是压缩完成信号。适配器只保存
触发方式，不把摘要或其他 transcript 元数据写入前端事件。带 UUID 的边界幂等记录，
`session_compaction_epoch` 从持久边界重建；下一轮若源 Run 的 epoch 已变化，
`context_protocol` 发送完整业务上下文刷新。自动压缩边界在工作台执行流中提示。

用户按“压缩上下文”时，服务端只允许对当前最新、已完成、没有排队插话的 SDK 会话
创建维护 Run。它保留原项目、Agent、Provider 和已认证主会话的身份绑定，持有同一
SDK session 锁；适配器发送精确 `/compact`，不给模型任何工具、MCP 或 Plugin Skill。
此 Run 不扫描产物，也不写任务日记/记忆。只有收到 `trigger=manual` 的 SDK 压缩
边界才判定成功。缺少确认或中途崩溃时，下一轮强制完整业务上下文刷新。

离线测试覆盖用量字段与累计用量区分、手动边界确认、重复边界、自动边界回放、
崩溃窗口修复、同会话续接、跨身份拒绝与工作台刷新渲染。真实 Provider 对 `/compact`
和 `get_context_usage()` 的行为、浏览器界面以及长时运行仍需验收。

依据：[SDK Python API](https://code.claude.com/docs/en/agent-sdk/python)、
[SDK 流式输出](https://code.claude.com/docs/en/agent-sdk/streaming-output)、
[Claude Code `/compact` 命令](https://code.claude.com/docs/en/commands)。
