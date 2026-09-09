# Agent-eval-kit 对照与接入方案

日期：2026-09-09。状态：代码对照已完成；以下平台接入为建议方案，尚未实施。

## 结论

当前 `127.0.0.1:8765` 是 Gold 人工标注工具。Open-Acciowork 已有运行日志、
确定性评分和真端点评测脚本，但没有将这些能力组织成统一的评测工作台。
Agent-eval-kit 的已接通前端与后端提供了更完整的实验、trace、复核和对比工作流。
改进应围绕这些工作流与真实数据接入，而不局限于调整标注页面样式。

建议复用 Agent-eval-kit 的平台结构和已接通功能，在评测侧增加 Open-Acciowork
适配器。保留本仓的任务规范、snapshot grader、gold 判定口径和产品运行时边界。

## 对照依据

- 用户指定参考：`https://github.com/Janettbella69/Agent-eval-kit`。
- 实际读取同一 origin 的本地 `agent-eval-kit/`，HEAD 为 `4d48e4b`；网页抓取未成功，
  因此本记录不声称代表远端最新版本。
- Open-Acciowork 以当前工作区为准。这里只读实现、报告索引和文件存在性；
  没有运行模型、生成 gold 标签或验证旧平台的完整端到端功能。

| 工作流 | Agent-eval-kit 的实现依据 | Open-Acciowork 当前情况 | 应学习的部分 |
|---|---|---|---|
| 实验管理 | `frontend/src/pages/ExperimentsPage.tsx`、`backend/api/experiments.py` | `backend/scripts/run_l2.py` 与 `evals/reports/` | 统一实验列表、任务结果和运行状态入口 |
| Trace 浏览 | `frontend/src/pages/TraceListPage.tsx`、`TracePage.tsx`、`TraceTimeline.tsx` | 主工作区可看单次运行事件；独立标注页面没有 trace 列表 | 搜索、筛选、事件时间线、工具输入输出和原始事件详情 |
| 人工复核 | `backend/api/traces.py`、`frontend/src/pages/TracePage.tsx` | 已有逐陈述 gold 标注；业务反馈另有复核入口 | 在 trace 上进入复核队列、记录开放编码、回到来源 |
| 实验对比 | `frontend/src/pages/ComparePage.tsx`、`storage/queries.py:compare_experiments` | 已有报告可人工对照，缺交互入口 | 按相同 case 对照前后结果，直接定位退化样本 |
| 校准 | `graders/validation.py` 与 `scripts/calibration_loop.py` 等 | judge 草稿、抽样和人工标签工具已有；校准仍待完成 | 人工与 judge 分歧、混淆矩阵、版本与样本划分记录 |
| 系统边界 | `runner/collector.py` 经 HTTP 调用被测系统；独立 SQLite | 核心 grader 独立；L2 driver 导入主后端，默认数据目录未隔离 | 独立评测存储与被测实例，通过显式数据契约连接 |

## 参考项目需要分开看待的部分

1. `frontend/` 已调用真实 API，包含实验、trace 详情和对比等功能，应作为功能参考。
2. `frontend-v2/` 可参考导航与视觉组织，但尚不能视为完整实现：
   `pages/observation/TracePage.tsx` 没有请求数据，默认展示固定网络错误；
   `DatasetsPage.tsx` 是空状态；`EvaluatorsPage.tsx` 含固定统计值。
3. 旧采集器固定访问 `/api/internal/research`，解析 `text_delta`、`product_found`、
   `sources` 等购物研究事件；并非改 `PRODUCT_API_URL` 就能接入本项目。
4. 部分脚本保留拆仓前的路径。例如 `scripts/calibration_loop.py` 将目录计算到
   工作区下的 `eval/`。复用前需修正路径并完成回归，不能仅按 README 假定可运行。
5. 购物场景的评分器、权重与阈值不能直接用来判跨境商业任务。旧平台的
   L0/L1/L2 与本仓 L1/L2/L3 命名也不同，应以实际判定含义映射。

## 建议的信息架构

评测工作台以实验列表为默认入口，导航包含：实验、Trace、数据集、评分器、
人工复核、对比与校准。Gold 标注是人工复核内的一项功能。

Trace 详情优先展示：任务输入、最终结果、执行时间线、工具输入输出、证据、
产物、授权与错误、评分依据、人工复核。保留 `run_id`、`trace_id`、事件 ID
和序号，能够从一条失败 finding 跳回对应事件或 snapshot 字段。

列表明确区分三种状态：运行是否完成、确定性检查是否通过、质量是否经过人工或
已校准 judge 评定。未标注、未知成本和缺失日志各自显示，不能用零分或成功代替。

## 接入契约

| 本仓数据 | 评测平台中的含义 | 接入要求 |
|---|---|---|
| `overall.json` 与各任务结果 | 一次实验及各 case 的执行记录 | 保留报告来源、任务 ID、运行 ID、执行结果；旧报告缺失版本时明确标注未知 |
| `events.jsonl` | 原始 trace 与事件时间线 | 原始脱敏事件不改写；展示层适配本仓事件类型，按 sequence 排序 |
| `snapshot.json` | 该次评分的冻结输入 | 保留 schema/profile 版本和内容 hash；不要用当前项目账本冒充历史快照 |
| `snapshot-eval.json` | 确定性 findings | 展示接受/拒绝、finding code 与字段路径，不擅自换算为百分制质量分 |
| Run 的模型/Skill 快照与 telemetry | 实验配置及可观察运行指标 | 缺失保持 null；费用显示实际记录的估计口径 |
| Gold batch、item、label、rationale、split | 逐陈述人工标注 | 保留 item 粒度；不能折叠为整条 trace 的单一 human_pass |
| 显式导出的业务反馈候选 | 待复核队列 | 保留原因与精确来源，不自动生成 gold 标签 |

本仓 `fact-inference-confusion` 的 `label=true` 表示“存在混淆”，其方向与
coverage/sourced 相反。映射到平台时必须记录判定语义，不能统一解释成 Pass。

实验对比应先检查任务内容、fixture、评分器版本与模型/Skill 配置，明确展示
哪些条件发生变化。case 增删单列；缺失历史字段不能推测补全。旧报告能供浏览，
但没有版本证明时不能称为严格可比实验。

## 实施顺序与验收

1. **历史浏览接入。** 在评测侧导入显式选择的报告和日志副本，以独立存储建立
   实验与 trace 索引。重复导入幂等；损坏或缺失文件明确显示；原文件保持不变。
   验收：无需模型调用即可从实验列表打开真实事件、结果与已有评分。
2. **复核与对比。** 接通逐陈述标注、开放编码和按 case 的前后对照；沿用 gold
   语义和负责人标注要求。验收：保存后刷新可恢复，模型分析不进入人工标签。
3. **新实验执行。** 通过独立适配器创建项目、上传 fixture、启动 Run、订阅 SSE，
   并处理取消、错误及授权。被测实例使用专用 `OPHIRA_DATA_DIR`；产品代码不导入
   评测平台，平台不把评测脚本加入产品请求链路。
4. **校准和发布判断。** 接入本仓三个 judge 的版本化判定与校准报告；阈值依据
   本项目人工 gold 确定。未校准维度展示为参考结果，不作为发布门禁。

首轮可用数据：本地现有 3 组 L2 `overall.json`，共 9 条任务运行记录；相应
`backend/data/runs/<run_id>/events.jsonl` 均存在。现有 1 个标注批次包含
9 次运行的 162 个判定条目。另有 4 个基准任务与 61 个离线 snapshot case。
这些数量不是模型质量指标，也不代表 162 条独立任务。

旧 L2 报告目录未发现直接位于各任务目录的 `events.jsonl`，历史导入应明确使用
本地已保存的运行日志来源，不能声称报告归档本身已包含完整 trace。

## 本次要求清单

| 要求 | 状态 |
|---|---|
| 学习用户已有项目的架构与交互工作流 | delivered：对照上述前端、后端和脚本源码 |
| 说明当前体验落差及可复用部分 | delivered：明确独立标注工具与统一评测平台的差别 |
| 给出本项目的接入边界、数据映射与实施顺序 | delivered：本方案 |
| 修改平台或主系统运行逻辑 | deferred：本次先完成参考分析；实施范围尚待确定 |
| 运行付费评测、自动标注或替换历史数据 | omitted：不属于本次参考分析 |
