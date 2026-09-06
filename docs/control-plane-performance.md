# 控制面性能基线

2026-09-06 UTC 的离线 ASGI 基线通过 PRD v2 §3.9 三项控制面延迟门槛。
这次测量使用实际 FastAPI 路由、中间件、FileLedger、SkillRegistry 和 RunManager，
运行器是显式的 `OfflineWaitingRuntime`，仅等待真实 RunManager task 被取消。
结果只证明本机进程内控制面路径，不证明真实 SDK 进程中断、网络或 SSE 客户端交付性能。

## 实测结果

原始归档：[report.json](../evals/reports/control-plane-20260906T051626Z/report.json) 与
[samples.jsonl](../evals/reports/control-plane-20260906T051626Z/samples.jsonl)。
正式测量执行一次，没有通过调整门槛、停掉同机工作负载或重复选取更快结果改善结论。

| 场景 | 正式样本数 | 创建 API P95 | 首排队/启动事件持久化 P95 | 取消生效 P95 | 失败样本 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 单并发 | 100 | 11.158 ms | 9.758 ms | 4.272 ms | 0 |
| 4 并发 | 100 | 42.108 ms | 35.081 ms | 15.281 ms | 0 |
| PRD 门槛 | — | < 500 ms | < 2,000 ms | < 2,000 ms | — |

每组另有 10 个预热样本，全部保留在原始文件中，不混入正式分位数。
单并发预热三项 P95 分别为 29.155、27.643、4.159 ms；4 并发分别为
62.232、54.825、15.104 ms。两组预热均零失败。

环境：Darwin 25.6.0、arm64、18 个逻辑 CPU、Python 3.11.15、FastAPI 0.141.1、
Starlette 1.6.0、HTTPX 0.28.1、Pydantic 2.13.4。已安装 Claude Agent SDK 0.2.143，
本次未执行 SDK。测量期间为共享开发机，其他任务的 CPU、存储和进程负载没有隔离。
运行开始/结束时间为 `2026-09-06T05:16:26.509531Z` / `2026-09-06T05:16:30.084509Z`。

归档记录了 Git HEAD、测量前后后端源码与脚本 SHA-256、13 个实际 Skill 包的版本和内容哈希。
源码在本次测量期间未变；工作树内容由文件哈希标识，不能仅凭 HEAD 将结果归属于未包含这些改动的提交。
环境信息不包含主机名、用户名、绝对工作区路径、环境变量值或密钥。

## 测量边界

- **创建 API**：在调用 `AsyncClient.post` 前使用 `perf_counter_ns()` 打点，直到完整
  ASGI HTTP 响应返回。包含真实路由、中间件、Skill 快照、文件写入和响应序列化。
- **首事件持久化**：起点与创建 API 相同。包装当前 EventPublisher 实例的 `publish`，
  `await` 原实现返回后立即记录第一个 `run.created/status=queued` 或
  `run.started/status=running` 事件。该时间晚于实际 append 与通知；不从 HTTP 响应时间
  或事件墙钟时间反推，也不称作 SSE 已到客户端。
- **取消生效**：调用取消 POST 前打点，终点取真实 RunManager task 的 done callback、
  runtime `finally` 退出和 `run.cancelled` publish 返回三者中的最晚时间。另从真实账本读取
  `cancelled` 终态，核对所观测事件的序号、类型和状态，并确认 runtime 收到取消调用。
  HTTP 取消响应时间单独保留，不能单凭 200 响应判定 task 已退出。
- **存储与并发**：每组使用一个新的临时数据目录、一个持续增长的项目和从当前仓库复制的
  Plugin。SkillRegistry 逐次创建真实不可变快照。单并发是一条连续请求序列；4 并发是
  4 条同时运行的请求序列，每条在创建、启动、取消和核对完成后发出下一次请求，正式样本
  每条 25 个。使用同一事件循环和同一项目，未构造多 worker 或远端存储场景。
- **离线约束**：通过 `Settings.model_construct` 只使用显式路径与声明默认值，不加载
  环境变量、`.env` 或 secret 文件；隔离 `main.py` 导入时的默认 app 初始化。HTTPX 设置
  `trust_env=False`，仅使用 ASGITransport。执行期间对外 socket/DNS、子进程和 vault 读取
  均有失败即停止该操作的保护。没有 Provider profile，也没有 SDK 或模型调用。
- **不包含**：应用初始化耗时、网络/TLS/代理、SSE 运输及断线、模型首 token、SDK 原生
  interrupt 与子进程退出、跨 worker 取消、trusted-gateway 鉴权、后台 scheduler/channel/push
  服务、长期生产负载。本次不启动应用 lifespan 后台服务。真实部署仍需在这些边界上独立验收。

## 统计与失败处理

采用 nearest-rank：排序后取 `ceil(N × percentile / 100) - 1`，不是插值。
报告保留 P50、P95、P99 和最大值，门槛使用严格的小于比较。

所有预热与正式样本都进入 JSONL，包括 HTTP 失败、超时、事件缺失、任务退出异常及
未执行的计划样本。失败仍保留已测得的时间、HTTP 状态和结构化错误代码；不写响应正文或异常消息。
某指标未完成时在分位数排序中占据正无穷位置，JSON 以 `null` 与 `p95_unbounded` 表示
无法得到有限 P95 的情况；即使失败比例低于 5% 且 P95 仍有限，任何失败也会使该组不通过。
后续场景或清理异常不会删除已完成的原始样本。

全部 220 个原始样本包含相对单调时钟时间、Run ID、事件序号与类型、真实终态核对结果。
`report.json` 的 `raw_samples_sha256` 对完整 JSONL 字节做校验。
每组少于 100 个正式样本或没有预热时，程序明确标为不具备正式验收资格。

## 复现和验证

从仓库根运行：

```bash
PYTHONPATH=backend/src .venv/bin/python backend/scripts/benchmark_control_plane.py --samples 100 --warmup 10
PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_control_plane_benchmark.py -q
.venv/bin/ruff check backend/scripts/benchmark_control_plane.py backend/tests/test_control_plane_benchmark.py
```

基准默认创建新的 `evals/reports/control-plane-<UTC>/`，可用 `--output-dir` 指定尚不存在的目录。
`--timeout-seconds` 默认 10 秒，只限制单次等待，不改变 PRD 延迟门槛；协作式事件循环超时
不能抢占阻塞的同步文件调用。CLI 退出码 0 表示本次离线基线通过，失败退出 1 且仍写归档。

测量器自身的 11 项测试已通过，覆盖真实 API/账本/Skill 生命周期、publish 后打点、
响应延迟与事件时间分离、取消等待 task 清理、严格阈值、失败样本分位数、第二组失败留存、
临时目录清理失败留存，以及网络/子进程/vault 读取阻断。相关 Ruff 检查通过。
这些测试验证测量方式；表中的延迟来自上方正式运行归档。
