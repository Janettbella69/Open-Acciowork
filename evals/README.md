# OPHIRA 评测体系（PRD v2.1 §3.11 / §6.6）

OPHIRA 的差异化承诺是"可审计"；这套评测把承诺变成可度量、可回归、可对外出示的工程事实。
评测产物（基准任务集、失败模式分类法、judge 校准报告、每版本评测报告）随版本发布。

## 三层金字塔在本仓的落点

| 层 | 对象 | 本仓落点 | 触发 |
|---|---|---|---|
| L1 确定性检查 | 结构与安全不变量 | `backend/tests/` + `snapshots/core-commerce-v1.json`（61 个离线正例/恶意变异 case；执行器见 `tools/snapshot_eval.py`） | 每次 commit（CI） |
| L2 行为与能力 | 端到端任务轨迹 | Green Path harness + `tasks/*.yaml`；`run_l2.py` 驱动真端点、物化完整 snapshot，并执行任务的 `minimum_pass.profile` | 每 PR 手动 / 每日（真端点，花钱，环境变量门控） |
| L3 质量判定 | 结论与证据质量 | `evals/judges/*.md`（prompt 草稿）。**未经 gold 校准（TPR/TNR）之前只是参考信号，不得进入发布门禁** | 版本发布 + 线上抽样（待校准后） |

## 建设顺序（错误分析先行，不先写 judge）

1. ✅ 基准任务集 v1：`tasks/` 覆盖场景 A（需求侧/供给侧两个起点）、B（单品验证）、C（供应商询盘），合成 fixture 均已标注 synthetic；四个自然语言 `minimum_pass` 已转换为可执行 snapshot profile。
2. ✅ Snapshot grader 首批门禁：61 个数据驱动 case 覆盖正反例、provenance bypass、stale apply、fake grounded metric、empty/timeout/malformed/concurrency/cross-capability；静态 case 明确是离线契约，不冒充真实 E2E。
3. ⬜ 错误分析：跑基准集收集 traces（`run_l2.py` 的 bundle 输出），人工开放编码归纳失败模式 → `failure-modes.md`；能用代码断言的失败模式回流到 L1/L2，不上 judge。
4. ⬜ Judge 编写与校准：`judges/` 草稿已给出；gold 集**由项目负责人本人标注**（口径见 `gold/README.md`），划分 train/dev/test，汇报 TPR/TNR。
5. ⬜ 回归化：Provider 合约测试（`backend/tests/contract/`，已建成并出过 DeepSeek 报告）、SDK 升级、Skill 版本变更、系统提示词修改必须重跑 L1+L2。
6. ⬜ 线上线下同口径：§3.10 护栏指标与离线 judge 共用同一套定义与 prompt。

## 运行方式

```bash
# L1（离线，免费，CI 自动跑）
PYTHONPATH=backend/src .venv/bin/pytest backend/tests

# Snapshot release gate（离线，免费；61 个正反/恶意变异 case）
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py

# GUI browser acceptance（真实 Chromium/Chrome + 公网；不调用模型、不产生模型费用）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_e2e.py \
  --url https://1.1.1.1/cdn-cgi/trace
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_l2_e2e.py \
  --launch-chrome --cdp-endpoint http://127.0.0.1:9337 \
  --url https://1.1.1.1/cdn-cgi/trace

# Green Path 出口标准（真端点，花钱）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_green_path.py --provider deepseek --rounds 3

# L2 基准集（真端点，花钱；会执行 minimum_pass profile 并写 snapshot bundle）
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --dry-run
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --task b-product-validation --provider deepseek

# Provider 合约矩阵（真端点，花钱）
OPHIRA_CONTRACT_PROVIDER=deepseek PYTHONPATH=backend/src .venv/bin/pytest backend/tests/contract -v
```

报告统一落 `evals/reports/`（已含 Green Path 3 连过 bundle、2026-09-04 基于
Claude Agent SDK 0.2.143 的 DeepSeek 真端点合约复测报告及
2026-09-03 的真实 browser L1/L2 acceptance 报告）。Browser 报告仅证明浏览器接入、
安全门和 Evidence/Artifact/receipt 持久化，不等同于真实 Provider 行为评测或 GUI L3。
Snapshot schema、case 范围和“离线不等于真实 E2E”的边界见
[`snapshots/README.md`](snapshots/README.md)。

## 门槛口径模板（数值留空，跑出基线后填定，不预填演示数字）

- L2 基准集任务完成率 ≥ __%；单任务成本 P95 ≤ __ USD、时长 P95 ≤ __ min
- 证据覆盖率 ≥ __%；无来源具体数据率 ≤ __%；事实/推断混淆率 ≤ __%
- Judge 校准门槛：TPR ≥ __、TNR ≥ __；未达标的维度不得进入 §6.6 发布门禁

## 评测代码归属口径（2026-08-24 澄清）

**边界原则不变**：产品包与评测系统不耦合，`ophira_backend` 永不 import `evals`。
边缘 driver 有两个显式接触点：`sample_gold.py` 只读 storage；`run_l2.py` 把运行时状态
序列化后调用纯 stdlib grader。CI 用不安装 backend 包的独立 job 证明 grader 自身独立。

三类代码的归属：

| 代码 | 位置 | 理由 |
|---|---|---|
| gold 抽样/标注工具链 | `evals/tools/` | 纯评测资产，`gold_sampling.py`/`gold_annotator.py` 只用 stdlib |
| 基准任务/judge/口径/报告 | `evals/` | 纯评测资产 |
| L2 runner（`run_l2.py`）与 trace 摘要（`digest_run.py`） | `backend/scripts/` | 是**驱动主系统跑起来**的脚本（重度 import ophira_backend，与 `run_green_path.py` 同类），不是评测判定逻辑；判定口径全部住 `evals/`。不随产品打包，不进 `ophira_backend` 包 |

判据：**判定"好坏"的逻辑只能住 `evals/`；驱动"跑起来"的脚本住 `backend/scripts/`。**
两边都不允许被产品运行时 import。
