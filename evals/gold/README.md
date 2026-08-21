# Gold 标注集与 judge 校准口径（模板 · 数值留空）

> 规则（PRD §3.11）：**gold 由项目负责人本人标注**，不外包、不由模型代标。
> 本目录当前只有口径模板；标注数据尚未产生。

## 流程（工具见 `evals/tools/README.md`）

1. 从 `evals/reports/` 的 L2 bundle 中抽样运行（首批建议 ≥30 条，覆盖 4 个基准任务）：
   `.venv/bin/python evals/tools/sample_gold.py` → 样本批落 `gold/samples/batch-*.json`。
2. 逐条按各 judge 的判定问题人工标注（是/否 + 一句话理由），存 `gold/<judge-name>/labels.jsonl`：
   `{"run_id": "...", "item": "...", "label": true, "rationale": "..."}`
   （工具会附加 item_id/judge/split/batch/labeled_at 等校准用字段；`label=true` 语义
   按 judge 输出字段：covered / sourced / confusion）。
   标注界面：`python3 evals/tools/annotate_gold.py` → http://127.0.0.1:8765。
3. 划分 train（写 prompt 时可看）/ dev（调 prompt）/ test（只用于最终汇报），建议 4:3:3
   （抽样时已按 judge 分层预分配 `split` 字段，避免标注后挑选带来的偏置）。
4. 用 judge 跑同批数据，对照 test 集汇报 TPR / TNR / 一致率；偏差样本回流修订 judge 口径。
5. 校准报告存 `gold/<judge-name>/calibration-report.md`，含 prompt 版本哈希。

## 校准门槛（跑出基线后由负责人填定，不预填演示数字）

| Judge | TPR ≥ | TNR ≥ | 状态 |
|---|---|---|---|
| evidence-coverage | __ | __ | 未校准 |
| unsourced-claims | __ | __ | 未校准 |
| fact-inference-confusion | __ | __ | 未校准 |

**未达标（或未校准）的 judge 输出不得作为发布依据（§6.6）。**
