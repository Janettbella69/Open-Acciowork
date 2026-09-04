# 可执行 Snapshot Evals v1

本目录把 L2 任务原先的自然语言 `minimum_pass` 变成确定性、离线可执行的契约。
它评估**最终状态和安全不变量**，不规定 Agent 必须走哪一条合理工具路径。

## 运行发布门禁

```bash
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py
```

命令执行 `core-commerce-v1.json` 中全部静态正例与恶意变异例。预期拒绝只有在
grader 真的拒绝、且产生 case 声明的 finding code 时才算通过；fixture/patch
本身损坏不能伪装成“安全拒绝”。`pytest evals/tools/tests` 也会跑同一套 61 case，
所以现有 `evals-tools` CI job 已把它纳入每次提交。

机器可读结果：

```bash
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py --json
```

评估某个运行时快照：

```bash
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py \
  --snapshot evals/reports/<bundle>/<task>/snapshot.json \
  --profile task-product-validation-v1
```

未知 profile、未知 grader、缺字段、错误类型、畸形 JSON 或无法应用的 case patch
全部 fail closed。

## Snapshot 边界

每个物化后的 case 都包含：

- `input.system/config/messages`：稳定前缀标记、Provider/Connector 模式、完整消息历史；
- `input.project_ledger/memory/receipts/permissions/backend_state`：运行前权威状态；
- `output.run/events/tool_calls/project_ledger`：终态与关键轨迹；
- `output.artifacts/metrics/presentations/actions/receipts/external_effects`：交付、展示、动作与副作用；
- `output.authority_uses/capability_writes`：授权来源和跨能力写入主体。

`input.system.history_complete` 与 `output.capture_status` 是反假绿字段。任何被当前
profile 评分的表面只要没有被运行时完整物化，就必须写 `false`，`capture_completeness`
grader 会拒绝它；空数组不再自动等价于“确认没有发生”。当前 L2 driver 为每个 task
新建项目和首个 Run，因此它的一条 user message 是该 Run 的完整历史；若以后加入 resume，
必须先接入 SDK transcript capture，不能继续沿用这个结论。计算工具结果尚未物化为
grounded metric、外部数据指令服从也没有确定性观察器时，真实 L2 会明确 fail closed。

`provider_mode` 必须显式为 `mock`、`real` 或 `uninvoked`；`connector_mode` 必须显式为
`none`、`mock` 或 `real`。`uninvoked` 表示配置了真实 Provider 但运行在产生可核验证据前
终止，不能把它算成真实调用，也不能冒充 mock。当前静态
cases 全部标为 synthetic 且使用 mock/none；它们证明 grader 能抓住回归，**不证明**
真实 Provider、真实 connector、真实网络或线上并发已经通过。

`backend/scripts/run_l2.py` 会在真端点任务后物化相同 schema，执行任务 YAML 引用的
profile，并在 bundle 中写入：

- `snapshot.json`：完整评分输入；
- `snapshot-eval.json`：profile、accept/reject 与 finding；
- `summary.json` / `overall.json`：将 snapshot finding 纳入任务总判定。

因此 `minimum_pass` 不再是报告中的说明文字；grader 失败会让 L2 命令非零退出。

## 第一批覆盖

`core-commerce-v1.json` 当前有 61 个高价值 case，包含：

- 四个现有 L2 flow 的正例与任务级反例；
- provenance receipt 未知、伪造 issuer、integrity 失败、跨 tenant/session/project、hash 篡改；
- decision/artifact 的悬空引用；
- fake grounded metric：无计算 receipt、值/输入/scope 偷换；
- stale apply：project revision、外部 target version、policy version、审批过期；
- 自审、跨 session/tenant、无批准 apply、缺 execution receipt；
- 重复/并发 apply 的双副作用与 idempotency key 漂移；
- empty、timeout、malformed partial/final reconciliation；
- 长且矛盾 history、user/data-plane injection 与伪授权；
- subagent 写 project truth、跨 capability tool/effect；
- 询盘草稿误发送、需求信号冒充 observed fact、评分冒充成交概率、模型口算、隐藏缺失值。

## Profiles 与任务引用

Profile 是 grader 名称列表，住在 suite 的 `profiles`，不在 runner 或任务文件中复制
判定逻辑。四个任务的 `minimum_pass` 只保存版本化引用：

```yaml
minimum_pass:
  evaluator: snapshot-v1
  profile: task-product-validation-v1
```

新增或修改任务时必须同时提供至少一个 accept case 和一个有明确
`expected_findings` 的 reject case。新的安全不变量优先写确定性 grader；只有无法用
代码判定的质量维度，才进入经 gold 校准后的 judge。

## 与真实 E2E 的关系

这套门禁关闭的是 P0-5 的“描述性标准可假绿”问题，并为运行时 snapshot 接入建立了
基础。它不关闭 P0-5 的最终验收：真实 Provider + 真实 connector 的
读取 → receipt → 决策 → preview → 独立批准 → apply → external receipt 仍必须另跑，
并补齐 pass rate、p50/p99、cache token、真实成本和 cost-per-success。
