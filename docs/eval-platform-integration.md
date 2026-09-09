# 评测平台历史数据接入

日期：2026-09-09。代码落在独立的 `agent-eval-kit/`，没有修改产品包、SDK 运行时或
现有 Gold 标注文件。接入覆盖参考方案的历史浏览、人工复核与对比部分。

## 可用入口

本地工作台：**http://127.0.0.1:5200/acciowork/experiments**。

从 `agent-eval-kit` 根目录启动归档 API，再启动其 `frontend/`：

```bash
PYTHONPATH=backend .venv/bin/python -m adapters.open_acciowork serve
# 另一个终端
cd frontend
npm run dev -- --host 127.0.0.1
```

完整平台的 `backend/main.py` 也挂载相同 API；二者选择一个启动即可。
导入命令、依赖和协议详见 Agent-eval-kit 的
[`examples/open-acciowork/README.md`](https://github.com/Janettbella69/Agent-eval-kit/blob/main/examples/open-acciowork/README.md)。

## 实际接入

- 独立 SQLite 中已导入 3 组 L2 报告、9 次任务运行、133,342 条原始事件。
  原始输入文件按 SHA-256 保存副本；相同输入重复导入不会新增实验。
- 实验列表可进入任务 Trace，阅读任务输入、最终结果、工具输入输出、授权和错误。
  时间线保留原事件 ID、run_id、trace_id、tool_use_id 和 sequence，可筛选、搜索与定位。
- 162 条已有候选陈述可按 judge、split 和标注状态进入复核；三种 Judge 的正负类
  语义保持不变，尤其 `fact-inference-confusion` 的 true 表示存在混淆。
  人工标签、理由、开放编码和复核状态持久化；标签可按原 Gold JSONL 字段导入/导出。
- 实验对比显示两侧 Trace、Case 增删与版本字段状态。Fixture、评分器等历史版本
  未记录时标记“可比性不足”，不据旧 passed 字段宣称改善或退化。

这 9 次历史运行没有当前格式的冻结 `snapshot.json` / `snapshot-eval.json`。
因此 Snapshot 检查显示“未评分”；旧报告执行通过单独展示。证据与产物来自当时
保存的 Gold 抽样档案，界面明确标注这一来源。没有读取当前业务账本来重建历史评分。

新保存的标签落在 Agent-eval-kit 的独立评测库；现有 `evals/gold/` 不会被覆盖。
负责人可下载 JSONL 按现有校准流程使用。模型没有替负责人标注任何真实候选。

## 验证口径

29 项适配器离线测试通过，覆盖幂等副本、文件缺失/损坏/截断、事件序号和查询、路径隔离、
Gold 正反方向及导入导出、乐观并发检查、开放编码、版本对比和本机 API 保护。
前端执行 ESLint 和 TypeScript/Vite 构建。

真实归档的浏览器验收覆盖实验导航、结果、工具输出、sequence 定位、证据、
复核条目跳转、两侧对比与 Trace 筛选。表单保存及刷新恢复使用临时合成数据库，
与真实数据分开验证。浏览器截图只作为页面与交互验收，不代表模型质量达标。

## 要求清单

| 要求 | 状态 |
|---|---|
| 复用旧平台已接通前端结构，增加本项目适配器 | delivered：Agent-eval-kit 的 frontend 与 backend/adapters/open_acciowork |
| 真实实验 → Trace → 证据与产物 | delivered：3 个实验、9 个运行；明确历史快照缺失 |
| 逐陈述人工标注、开放编码、对比 | delivered：独立存储、刷新恢复、JSONL 导入导出、版本可比性检查 |
| 主产品与评测侧解耦 | delivered：无产品 import；仅显式读取报告、日志和候选档案 |
| 创建/批量执行新实验、订阅 SSE、取消和授权处理 | deferred：本轮先完成历史数据与复核工作流 |
| Judge 校准和发布判断 | deferred：仍需负责人真实 Gold、judge 运行与校准报告 |
| 调用付费模型或自动标注真实样本 | omitted：未执行，不属于本轮范围 |
