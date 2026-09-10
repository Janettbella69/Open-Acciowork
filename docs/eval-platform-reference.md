# Agent-eval-kit 工作流参考

本记录保留 2026-09-09 对用户指定
[Agent-eval-kit](https://github.com/Janettbella69/Agent-eval-kit) 的源码对照与设计参考。
**两个项目的评测系统各自独立。** Open-Acciowork 的实现位于本仓 `evals/workbench/`，
详见[实施记录](eval-platform-integration.md)；本参考不要求修改、安装或启动另一项目。

此前“在 Agent-eval-kit 中增加本项目适配器”的建议误解了参考范围，现已撤回。
学习对象是实验、Trace、人工复核和对比的组织方式；代码归属、依赖、数据库和运行进程
应由 Open-Acciowork 自己维护。

## 对照依据与启发

当时读取的是同一 origin 的本地参考仓库 `4d48e4b`，本记录不声称覆盖远端最新实现。
下表中的 Open-Acciowork 情况是参考分析阶段的观察；历史浏览、复核与对比现已在
本仓独立工作台接通，新实验执行与 Judge 校准尚未接入工作台。

| 工作流 | 参考源码 | 本项目应采用的方式 |
|---|---|---|
| 实验管理 | `frontend/src/pages/ExperimentsPage.tsx`、`backend/api/experiments.py` | 将本仓 `overall.json` 与任务结果组织为实验入口 |
| Trace 浏览 | `TraceListPage.tsx`、`TracePage.tsx`、`TraceTimeline.tsx` | 展示本仓原生事件，支持搜索、筛选、工具输入输出和原始事件详情 |
| 人工复核 | `backend/api/traces.py`、`frontend/src/pages/TracePage.tsx` | 从 Trace 进入逐陈述 Gold 与开放编码，保留精确来源 |
| 实验对比 | `ComparePage.tsx`、`storage/queries.py:compare_experiments` | 按 Case 对照结果，检查输入和版本条件，直接定位两侧 Trace |
| 校准 | `graders/validation.py`、`scripts/calibration_loop.py` | 本项目后续记录人工与 Judge 分歧、混淆矩阵、版本和样本划分 |
| 系统边界 | `runner/collector.py` 的 HTTP 采集、独立 SQLite | 使用本项目数据契约和独立存储，保持产品运行时与评测工作台解耦 |

参考仓库的 `frontend/` 是功能对照对象；当时 `frontend-v2/` 的若干页面仍含占位内容。
其采集器解析购物研究事件，评分器、权重、阈值和 L0/L1/L2 命名也带有原场景含义，
不能直接作为本项目判定口径。参考脚本的路径与运行状态仍需各自验证，不能由 README
推定全部可运行。这些差异也是本项目独立实现和验证的原因。

## 本项目的数据契约

| 本仓数据 | 工作台中的含义 | 保留要求 |
|---|---|---|
| `overall.json` 与各任务结果 | 实验及 Case 执行记录 | 报告来源、任务 ID、运行 ID、执行结果；缺失版本显示未知 |
| `events.jsonl` | 原生 Trace 时间线 | 原始脱敏字节、事件类型、ID 与 sequence；展示按序排序 |
| `snapshot.json` | 该次评分冻结输入 | schema/profile 版本与内容 hash，不用当前账本代替历史快照 |
| `snapshot-eval.json` | 确定性 findings | 接受/拒绝、finding code、字段路径，不换算为百分制质量分 |
| 模型/Skill 快照与 telemetry | 运行配置与可观察指标 | 缺失保持 null；费用说明估算口径 |
| Gold batch、item、label、rationale、split | 逐陈述人工标注 | 保留条目身份与划分，不折叠为整条 Trace 的单一 human_pass |
| 显式导出的业务反馈候选 | 后续待复核来源 | 保留原因和精确来源，不自动生成 Gold 标签 |

`fact-inference-confusion` 的 `label=true` 表示“存在混淆”，与 coverage/sourced 的
正类含义不同。界面必须显示各自判定语义。列表也应分别显示运行完成、确定性检查、
人工质量状态；日志缺失、成本未知或未标注均不能用成功或零分代替。

实验对比检查任务输入、Fixture、评分器、profile、schema、模型与 Skill 配置。
Case 增删独立展示，缺失历史条件不推测补全；归档可供人工对照，并不自动构成严格
可比的实验。

## 实施边界与后续工作

| 阶段 | 状态与验收口径 |
|---|---|
| 历史归档浏览 | delivered：本仓独立导入选定报告和日志副本；实验可打开原始 Trace 与已有评分 |
| 复核与对比 | delivered：逐陈述 Gold、开放编码、JSONL 导入导出、版本可比性检查 |
| 新实验执行 | deferred：未来经产品 API 创建项目、上传 Fixture、启动 Run、处理 SSE/取消/授权；被测实例使用专用数据目录 |
| Judge 校准与发布判断 | deferred：以本项目真实人工 Gold 校准三个 Judge；未校准维度不作为发布门禁 |
| 修改参考项目以承载本项目功能 | omitted：违反两个项目各自独立的边界，误加集成已撤回 |
| 付费评测与自动填写真实 Gold | omitted：参考分析与归属修复均未执行 |

工作台的启动、数据归属和验证以
[`evals/workbench/README.md`](../evals/workbench/README.md) 为准。
