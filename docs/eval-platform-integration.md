# 独立评测工作台实施记录

Open-Acciowork 的评测工作台现归属本仓 **`evals/workbench/`**，前后端、依赖、进程和
数据库独立维护。此前将专用页面和 API 写入 Agent-eval-kit 的实现已纠正；参考项目的
工作流设计不构成共用工程或数据库的授权。Agent-eval-kit 已撤回那次集成，其被改动的
受版本控制文件恢复到 `4d48e4b` 的内容。

## 启动与归属

本地入口：**http://127.0.0.1:5201/experiments**，API 位于 `127.0.0.1:8101`。
从 Open-Acciowork 根目录分别在两个终端执行：

```bash
cd evals/workbench
PYTHONPATH=backend .venv/bin/python -m acciowork_eval serve
```

```bash
cd evals/workbench/frontend
npm run dev
```

首次安装、导入命令、环境变量和构建预览见
[`evals/workbench/README.md`](../evals/workbench/README.md)。启动不需要 Agent-eval-kit，
也不加载产品模型运行时。原 `/acciowork/...` 链接仅在本工作台内重定向到独立路由。

## 数据迁移与已有能力

原专用数据库经 SQLite 一致性备份迁至 `evals/workbench/data/archive.sqlite3`。
迁移前后 9 张表的 schema、行数与内容摘要全部一致，SQLite 完整性检查通过。
迁移包含 3 个实验、9 次运行、133,342 条事件、31 份原文件副本、162 条候选陈述
及 162 条运行关联；迁移时人工标签、Trace 复核和复核历史均为 0。
恢复副本与逐表审计留在本机忽略的 `evals/workbench/data/` 下。

工作台可从实验进入任务输入、最终结果、事件时间线、工具输入输出、证据与产物。
保留事件 ID、`run_id`、`trace_id`、`tool_use_id` 和 `sequence`，支持筛选、搜索和定位。
已有候选陈述按 Judge、split 和标注状态进入人工复核；标签、理由、开放编码和复核
状态保存于本项目的数据库，Gold JSONL 可显式导入和导出。

这 9 次历史运行没有当前格式的冻结 Snapshot，因此确定性检查显示“未评分”，
旧报告执行判定单独展示。证据与产物来自当时保存的 Gold 抽样档案，未使用当前业务
账本重建历史评分。对比缺少版本证据时显示“可比性不足”。

产品文件、原始报告和现有 `evals/gold/` 保持原样；没有生成真实人工标签或调用付费模型。

## 验证范围

数据库逐表一致性已验证。34 项工作台后端测试、9 项前端测试和 59 项原有评测工具测试
通过；Ruff、前端 ESLint 和 TypeScript/Vite 构建通过。原有评测工具测试也使用工作台
自己的环境运行：在 `evals/workbench/` 执行
`PYTHONPATH=../tools .venv/bin/python -m pytest ../tools/tests -q`。

隔离 Chromium 验收通过两个独立入口、真实归档与 Trace、筛选与 sequence 定位、
复核队列、实验对比及旧链接重定向；新工作台没有页面异常或失败的本地请求。
生产构建预览中的标签保存、导出和复核连续保存/刷新恢复，仅使用临时合成数据库。
复核请求已修正为只发送可编辑字段，避免第二次保存时被只读审计字段触发 422。
本次[桌面截图](../evals/workbench/screenshots/experiments.png)与
[移动端截图](../evals/workbench/screenshots/experiments-mobile.png)随代码保存。

Agent-eval-kit 的恢复树与 `4d48e4b` 完全一致，原有前端 lint/build 及主要 API 冒烟检查
通过。额外检查发现该基线的开放编码接口已有列表序列化缺陷，本次恢复没有混入其修复。
运行完成、确定性检查通过、人工质量达标分别记录；本次拆分不提供新的模型质量结论。

## 要求清单

| 要求 | 状态 |
|---|---|
| 两个项目分别维护评测系统 | delivered：Open-Acciowork 工作台位于本仓，Agent-eval-kit 撤回误加的集成 |
| 前后端、依赖、进程和数据库独立 | delivered：本地 API 8101、前端 5201、自有配置与存储，无跨仓运行依赖 |
| 保留历史实验、Trace、人工复核和对比 | delivered：迁回原功能及归档数据，保留本项目 Snapshot/Gold 语义 |
| 迁移可恢复且不丢数据 | delivered：一致性备份、逐表核验、本地恢复副本与审计 |
| 主产品与评测侧解耦 | delivered：工作台不导入产品包，仅显式读取并复制选定档案 |
| 创建/批量执行新实验、订阅 SSE、取消与授权处理 | deferred：本轮修复归属与独立运行边界，尚未接入这些工作流 |
| Judge 运行、校准与发布门禁 | deferred：仍需负责人 Gold、Judge 执行和校准报告 |
| 调用付费模型或自动标注真实样本 | omitted：未执行，不属于本次修复范围 |
