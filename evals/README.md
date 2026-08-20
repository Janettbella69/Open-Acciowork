# OPHIRA 评测体系（PRD v2.1 §3.11 / §6.6）

OPHIRA 的差异化承诺是"可审计"；这套评测把承诺变成可度量、可回归、可对外出示的工程事实。
评测产物（基准任务集、失败模式分类法、judge 校准报告、每版本评测报告）随版本发布。

## 三层金字塔在本仓的落点

| 层 | 对象 | 本仓落点 | 触发 |
|---|---|---|---|
| L1 确定性检查 | 结构与安全不变量 | `backend/tests/`（架构静态门禁 `test_arch_gates.py`、事件脱敏 canary、权限边界、商业计算复算、Green Path 验证器离线测试） | 每次 commit（CI） |
| L2 行为与能力 | 端到端任务轨迹 | Green Path harness（`backend/src/ophira_backend/greenpath.py`）+ 基准任务集 `evals/tasks/*.yaml`，由 `backend/scripts/run_l2.py` 驱动真端点回放 | 每 PR 手动 / 每日（真端点，花钱，环境变量门控） |
| L3 质量判定 | 结论与证据质量 | `evals/judges/*.md`（prompt 草稿）。**未经 gold 校准（TPR/TNR）之前只是参考信号，不得进入发布门禁** | 版本发布 + 线上抽样（待校准后） |

## 建设顺序（错误分析先行，不先写 judge）

1. ✅ 基准任务集 v0：`tasks/` 覆盖场景 A（需求侧/供给侧两个起点）、B（单品验证）、C（供应商询盘），合成 fixture 均已标注 synthetic。
2. ⬜ 错误分析：跑基准集收集 traces（`run_l2.py` 的 bundle 输出），人工开放编码归纳失败模式 → `failure-modes.md`；能用代码断言的失败模式回流到 L1/L2，不上 judge。
3. ⬜ Judge 编写与校准：`judges/` 草稿已给出；gold 集**由项目负责人本人标注**（口径见 `gold/README.md`），划分 train/dev/test，汇报 TPR/TNR。
4. ⬜ 回归化：Provider 合约测试（`backend/tests/contract/`，已建成并出过 DeepSeek 报告）、SDK 升级、Skill 版本变更、系统提示词修改必须重跑 L1+L2。
5. ⬜ 线上线下同口径：§3.10 护栏指标与离线 judge 共用同一套定义与 prompt。

## 运行方式

```bash
# L1（离线，免费，CI 自动跑）
PYTHONPATH=backend/src .venv/bin/pytest backend/tests

# Green Path 出口标准（真端点，花钱）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_green_path.py --provider deepseek --rounds 3

# L2 基准集（真端点，花钱；--dry-run 只校验任务装配）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --dry-run
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --task b-product-validation --provider deepseek

# Provider 合约矩阵（真端点，花钱）
OPHIRA_CONTRACT_PROVIDER=deepseek PYTHONPATH=backend/src .venv/bin/pytest backend/tests/contract -v
```

报告统一落 `evals/reports/`（已含 Green Path 3 连过 bundle 与 DeepSeek 合约报告）。

## 门槛口径模板（数值留空，跑出基线后填定，不预填演示数字）

- L2 基准集任务完成率 ≥ __%；单任务成本 P95 ≤ __ USD、时长 P95 ≤ __ min
- 证据覆盖率 ≥ __%；无来源具体数据率 ≤ __%；事实/推断混淆率 ≤ __%
- Judge 校准门槛：TPR ≥ __、TNR ≥ __；未达标的维度不得进入 §6.6 发布门禁
