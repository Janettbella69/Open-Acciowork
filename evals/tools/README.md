# 评测工具（evals/tools）

评测系统自有代码，**与主系统解耦**：

- 逻辑与界面（`snapshot_eval.py` / `run_snapshot_evals.py` / `gold_sampling.py` / `gold_annotator.py` / `annotate_gold.py` /
  `annotate_gold.html`）只用 Python stdlib，不 import `ophira_backend`；
  测试 `tests/` 亦然（CI 中独立 job，不安装 backend 包）。
- 边缘 driver 有两个显式接触点：`sample_gold.py` 只读 import storage 加载 run；
  `backend/scripts/run_l2.py` 把运行时状态序列化后调用本目录的纯 stdlib grader。
  两者都不进入产品包或在线请求路径。
- 主包 `ophira_backend` 内不得出现评测代码；评测资产（任务集/judge/gold/报告）
  全部在 `evals/` 下。

## Gold 标注工作流（PRD §3.11 · gold/README.md）

```bash
# 1. 从 L2 报告抽样候选条目（确定性启发式，无模型调用；需 venv 装有 backend）
.venv/bin/python evals/tools/sample_gold.py            # 默认扫全部 evals/reports/l2-*

# 2. 起标注界面（stdlib，任意 python3 可跑），负责人本人逐条标注
python3 evals/tools/annotate_gold.py                   # http://127.0.0.1:8765

# 测试
.venv/bin/pytest evals/tools/tests
```

标签落 `evals/gold/<judge>/labels.jsonl`（按 item_id 幂等 upsert）；
跳过状态在 `evals/gold/annotation-progress.json`。界面只绑 127.0.0.1，勿对外暴露。

`label=true` 的语义与各 judge 输出字段对齐：
evidence-coverage → **已覆盖**（covered）；unsourced-claims → **有来源/合规**（sourced）；
fact-inference-confusion → **确属混淆**（confusion，正类）。

红线：gold 标签由项目负责人本人给出，不外包、不由模型代标——本目录工具只负责
抽样与记录。

### 用户反馈候选

`sample_feedback.py` 从显式导出的反馈 JSON 生成同格式的**无标签**批次；
只用 stdlib + `gold_sampling`，不 import backend，不读取业务数据目录，不调用模型。

```bash
.venv/bin/python evals/tools/sample_feedback.py \
  --input /path/to/feedback-candidates.json --out evals/gold/samples/feedback-batch.json
python3 evals/tools/annotate_gold.py --samples evals/gold/samples/feedback-batch.json
```

候选理由不是 gold verdict。失败/取消但没有商业陈述的条目保留在
`feedback_candidates`，不会因为零 `items` 被当成通过或丢弃。
显式导出前必须复核商业数据敏感性；脚本拒绝覆盖既有批次或写标签文件。
完整 API/人审流程及未完成人工门禁见 [`../../docs/feedback-loop.md`](../../docs/feedback-loop.md)。

## Snapshot release gate（P0-5）

```bash
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py
pytest evals/tools/tests
```

`snapshot_eval.py` 是纯 stdlib、fail-closed 的确定性 grader；静态 suite、schema、
profile 与 61 个 case 见 [`../snapshots/README.md`](../snapshots/README.md)。
`backend/scripts/run_l2.py` 只负责把主系统状态序列化成该公开 shape 并调用 grader；
所有“好/坏”判定仍只住在 `evals/`。
