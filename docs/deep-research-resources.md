# Deep Research：原生研究单元与运行资源

用户显式选择 Deep Research 后，同一个 Claude Agent SDK 主 Agent 可调用
`source-mapper`、`multilingual-researcher`、`conflict-verifier` 三个临时只读研究单元。
普通模式保持单主 Agent 和罕见的一次性只读支线，不注入固定研究团队。

## 调用与来源合同

三种定义来自 `constants.RESEARCH_UNITS`，使用 SDK `AgentDefinition` 和原生 `Agent`
工具。PreToolUse 与 can_use_tool 都核验调用范围，避免 SDK `allowed_tools` 自动允许
整类工具后绕过回调。每种研究单元每个逻辑 Run 只允许一次新建调用；后续只能通过
SDK `resume` 续接本 Run 原生 SubagentStart 已报告的对应 agent_id。未知类型、其他
Run 的 agent_id、类型不符或无法读取调用历史均被拒绝。

新建名额以首条持久 TOOL_STARTED 调用占用，同一个 tool_use_id 的原生重放可继续。
如果首次调用在子代理启动前失败，名额仍保守保留，不能据此自动再建第四个实例。
没有实际生命周期事件时，界面显示未观测到调用，不将三个定义当成三个已运行实例。
原生工具限制继续禁止子代理写文件、Bash、再委派、记忆提案、业务动作或账本修改。

Deep Research 启动配置不再为这些明确允许的只读工具重复设置 `ask` 规则。真实 SDK
验证发现，原来的启动审批配置把所有工具加入 ask，而研究单元使用 `dontAsk`，导致
即使 PreToolUse 返回 allow，Read/Glob/Grep 与只读账本工具仍全部被拒绝。修复保持
原有 PreToolUse/身份/路径审查和写入审批，并在钩子层明确拒绝子代理再委派；不切换
到绕过权限的模式。规则顺序见 [SDK 权限文档](https://code.claude.com/docs/en/agent-sdk/permissions)。

每个研究单元返回 JSON 来源包 `protocol="ophira-research-v1"`，包含 `claims` 与
`unknowns`。每条发现给出 claim、evidence_kind、source_locator、observed_at、excerpt、
language、conflicts、limitations；日期未知用 null，引用已有账本时保留 evidence_id。
翻译/转载指回同一原始来源，缺少出处的说法进入 unknowns。

来源包是 Agent 输出约定，不是可信来源凭证或新的数据库。主 Agent 必须复核原始
来源、按来源/定位/观察时间去重、保留日期/口径冲突，再调用既有
`record_evidence_batch`/`record_decision`。新 web/tool 证据继续要求当前 Run/session
服务器 receipt，上传文件继续经原有路径与文件来源验证；子代理文字不能绕过这些
服务端规则。每批最多20条，逐条验证，重复条目幂等返回；不承诺整批原子事务。

## 资源 API 与页面

`GET /api/runs/{run_id}/resources` 返回 schema_version `1`：

- 已固定的 SDK 估算金额上限、轮数政策、原始墙钟截止时间；默认轮数不设上限。
- Run 状态、已用时长、服务端剩余时长、最后已报告的 SDK 成本与 token 口径。
- 原生 SubagentStart/Stop 的实际 agent_id/type，与 TaskStarted/Progress/Notification
  的独立 task_id、调用关联、生命周期、最后工具和累计用量快照。
- 计算到的事件序号、观察时间、数据是否完整及不可用原因。

两种原生 ID 不假设相等；任务名称仅通过 SDK 报告的 tool_use_id 与实际 Agent
调用参数关联。Task 的 total_tokens/tool_uses/duration_ms 是累计快照，保留最后一次
有效观察，不累计每次进度，也不加到 Run 总用量。最终通知没有 usage 时保留带来源
序号的旧快照；回退或非法计数显示不可用。Run 结束不代表每个未报告结束的子任务成功。

API 使用项目 RUN_READ 权限，在读取前后复验，不缓存。事件回放上限为32MiB、
50,000条和1,000个任务/代理；不完整序号、身份冲突或超过上限时保留已固定的限制，
隐藏不可确认的用量。接口不读取原生 transcript，也不返回任务输出文件路径或推理文本。

工作区只在 Deep Research Run 显示资源面板，每5秒串行刷新，服务端相对时长经
单调时钟插值，避免用户设备日期偏差。切换项目/Run 时取消旧请求；权限/读取失败
后清除旧值，可手动刷新。终态停止自动刷新，取消按钮使用现有整个 Run 取消接口。
预算和截止时间由服务端/SDK执行，页面倒计时不承担执行控制。

金额始终为 SDK 估算，不能当厂商账单。没有结果消息时成本和剩余已报告额度为空，
不显示零；正在执行但尚未报告的消耗未知。Run token 优先采用完整 model_usage，
仅有 Result usage 时标明主 Agent 口径。相关 SDK 语义见
[用量文档](https://code.claude.com/docs/en/agent-sdk/cost-tracking) 与
[原生子代理文档](https://code.claude.com/docs/en/agent-sdk/subagents)。

## 验收

`backend/scripts/run_research_e2e.py` 复用真实 ASGI 控制面、原生 SDK runtime 与 Green
Path 审批驱动。只复制现有已验证 Provider 的配置与凭据到临时私有目录，使用三份
合成上传材料，禁止业务外发；任务结束后保存脱敏事件、资源采样、账本和已校验的
不可变报告字节，清理临时数据。默认 SDK 估算上限2美元、原始墙钟10分钟，不设轮数
上限。API成本并非实际账单限额，SDK通常在本次调用完成后才能判断超额。

离线回归覆盖调用限制、跨 Run 续接、并发重复、源身份、累计快照、缺失终态、
事件上限、权限复验、UI取消/刷新/切换，以及验收器拒绝缺少研究单元、子代理写入、
虚构引用、来源包缺失和未知成本。真实执行结果在交付清单另行记录。

此验收不代表开放互联网研究质量、真实商务来源、生产故障/长时预算、所有 Provider
能力或浏览器目视验收通过；这些范围仍需独立证据。
