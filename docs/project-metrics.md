# 项目控制面指标（PRD §3.9）

`GET /api/projects/{project_id}/metrics?days=7&max_runs=200` 是只读项目 API，
沿用 `run.read` 与当前项目成员鉴权，响应设置 `Cache-Control: no-store`。
不会调用 Provider、SDK 或外部服务，也不会增加日志管道。

## 时间窗口与覆盖

- `days` 默认 7，允许 1–31；`max_runs` 默认/最大 200，最小 1。
- `until` 可选，必须为带时区的 RFC3339 时间，不能晚于请求时间；服务端转为 UTC。
- 按 `Run.created_at` 选取 `[until - days, until)`，取最近的 Run，时间相同按 ID 排序。
  指标读取这些 Run 截至请求时已持久化的完整生命周期；`until` 不是历史状态回放。
- 每 Run 最多读取 4 MiB / 10,000 事件，每请求合计 32 MiB / 50,000 事件。
  字节边界最多额外读取 1 字节用于识别超限。超限或损坏时整份事件遥测记不可用，
  不把截断前缀冒充完整日志；Run 自身可观察的状态/时间仍可统计。
- Run 清单沿用文件账本扫描；事件重放和输出分组有界，不能据此宣称大规模索引查询性能。
  清单只统计可解析的记录，不承诺覆盖损坏或已删除的 Run 文件。
- 每维度最多 100 个已知分组；后续无法保留的分组进入 `null` 桶，并记录溢出关联次数。

## 响应类型

`schema_version` 为字符串 `"1"`。时间均为带时区 ISO 8601，比例为 0–1，费用为 USD 估计。
所有未知数使用 `null`，不能在 UI 中替换为 0。

```typescript
type Distribution = {
  observed_count: number; unavailable_count: number;
  observed_total: number | null; p50: number | null; p95: number | null;
};
type FailureRate = { numerator: number; denominator: number; value: number | null };
type RunMetrics = {
  run_count: number;
  status_counts: Record<string, number>;
  execution_failure_rate: FailureRate;
  duration_ms: Distribution;
  queue_duration_ms: Distribution;
  ttft_ms: Distribution;
  cost_estimate_usd: Distribution | null;
  cost_basis_counts: Record<string, number>;
};
type InvocationMetricGroup = {
  identifier: string | null; run_count: number;
  invocations: {
    invocation_count: number; completed_count: number; failed_count: number;
    unknown_count: number; pending_count: number; failure_rate: FailureRate;
    duration_ms: Distribution; queue_duration_ms: Distribution;
  };
};
type ProjectMetrics = {
  schema_version: "1"; project_id: string;
  window: {
    created_since: string; created_until: string; observed_at: string;
    selection_basis: "run.created_at";
  };
  coverage: {
    matching_runs: number; selected_runs: number; omitted_by_run_limit: number;
    invalid_created_at_runs: number; unavailable_event_replays: number;
    unavailable_reasons: Record<string, number>;
    event_bytes_read: number; event_count_read: number;
    unattributed_tool_events: number; unclassified_tool_invocations: number;
    group_overflow_associations: number;
    run_limit: number; per_run_event_byte_limit: number; per_run_event_limit: number;
    request_event_byte_limit: number; request_event_limit: number;
  };
  summary: RunMetrics;
  providers: Array<{
    dimensions: { provider_profile_id: string | null; configured_model: string | null };
    runs: RunMetrics;
  }>;
  models: Array<{ dimensions: { observed_model: string | null }; runs: RunMetrics }>;
  skills: InvocationMetricGroup[];
  mcp_tools: InvocationMetricGroup[];
  errors: Array<{ code: string | null; failed_run_count: number }>;
  business_outcome_available: false;
};
```

例如某窗口的 `summary.cost_estimate_usd` 为
`{"observed_count":2,"unavailable_count":1,"observed_total":0.6,"p50":0.2,"p95":0.4}`，
意思是两次已知估计共 0.6 USD，另一次费用未知；不代表三次总成本就是 0.6 USD。
这些是合成说明值，不是生产测量结果。空窗口各计数为 0，分位数/总费用/失败率为 `null`。

## 聚合口径与隐私

Provider 分组只使用冻结 `provider_snapshot` 的 profile ID 和配置模型；不查询当前配置，
不输出显示名、Base URL 或完整快照。模型分组复用已有 RunTelemetry 的已观察模型，
包括 SDK 初始化声明、Assistant 消息 model 和 result.model_usage 键；不能称为已证明实际回答模型，
也不以配置模型补缺。多模型 Run 可进入多个关联分组，因此分组 Run 数不能跨模型相加。

Skill 必须有 `tool.started`，且调用名匹配该 Run 的固定 Skill ID（无命名空间时仅允许唯一匹配）。
已安装或仅 `tool.planned` 的 Skill 不计调用。MCP 名须属于宿主注册工具常量。
每个 `(Run.id, tool_use_id)` 计一次，重复/冲突生命周期、未知名字进入未知统计；
缺调用 ID 的事件单列，不猜测配对。`tool.failed` 与明确 `isError=true` 计执行失败，
无法证明结局的调用不能计成功。`null` 分组表示未知/无法保留的标识符，不代表默认配置。

错误分组仅接受 `ProviderErrorCode`，优先 Run 的 typed code，其次一致的 `run.failed` typed code；
不根据错误正文分类。`SDK_RUNTIME_FAILED` 等现有枚举原样返回，缺失则为 `null`。

费用复用每 Run 最后一个累计结果；中途分段 `segment_*` 和同 Run 早期累计结果不重复相加。
相同 SDK session 中不同逻辑 Run 仍各计一次；不能用 session ID 去重独立查询。
崩溃后未知费用保持未知。成本标记 `sdk_anthropic_list_price` 或 `unknown`，不是实付账单。
模型分组 `runs.cost_estimate_usd=null`，Skill/MCP 不提供成本，避免把整 Run 费用重复归属。

P50/P95 使用 **nearest-rank**，只含可观察样本；时长单位为毫秒。
Run 执行失败率为 `failed / (failed + completed)`，取消和未结束另列且不进入分母；
工具率同样只含已证明 completed/failed 的调用。MCP 顶层 `isError` / `is_error`
仅接受布尔值，任一为 true 时计失败；两标记冲突或类型非法时计未知。
状态只描述执行结果，不证明产物有用、
Skill 使用正确或“有证据的下一步”达成；业务成功仍需用户确认，当前明确不可用。

聚合输出不包含 prompt、工具参数/结果、用户文本、原始错误、凭据、会话 ID 或 Run ID。
读取 Skill 名时只抽取经固定快照验证的标识符；异常标识符进入未知桶。
费用/工具缺失不会被推测补全；没有观察到调用不等于已证明没有执行。

## 工作区与验证

工作区的“项目运行统计”默认折叠，用户打开后才请求数据，可选最近7天或30天并刷新。
显示运行状态、P95、已观察SDK费用和各指标缺失数量，维度明细按需展开。
未知费用显示不可用，真实已记录的零费用仍显示零；有缺失时不把部分费用称作全部成本。
日志超限、无法分类与分组溢出均可见，模型分组明确提醒同一Run可能重复出现。

切换项目/时段、关闭面板或刷新会撤销旧请求并卸载旧数据；即使网络客户端忽略撤销，
迟到响应也不能覆盖当前面板。刷新遇到403会隐藏旧结果。API在中间件、处理器入口和
读取完成后重新核验当前成员权限，读取期间的撤销不能放出聚合结果。

本批冻结后：后端 **1112 passed / 8 live-only skipped**，前端 **201 passed**；
Ruff、ESLint、TypeScript、build和diff-check通过。主JS619.11kB，既有体积警告保留。
指标专项29项、存储预算专项59项，前端面板11项及客户端2项新增契约测试。
非空真实ASGI路由测试验证选定项目与其他项目的合成费用隔离、敏感文本排除及缺失值；
这些调用使用隔离账本与离线runtime，没有调用模型或外部服务。

尝试Browser目视验收返回 `No browser is available`，排查后浏览器列表为空；未绕过
工具边界，也未声称真实浏览器/手机显示已验收。SDK现金流真端点验收仍另行等待授权。
