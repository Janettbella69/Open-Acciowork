# 评测工具（evals/tools）

评测系统自有代码，**与主系统解耦**：

- 逻辑与界面（`gold_sampling.py` / `gold_annotator.py` / `annotate_gold.py` /
  `annotate_gold.html`）只用 Python stdlib，不 import `ophira_backend`；
  测试 `tests/` 亦然（CI 中独立 job，不安装 backend 包）。
- **唯一接触点**：`sample_gold.py` 以只读方式 import 主系统 storage 层加载 run
  数据（依赖方向 evals → backend 单向；主系统对 evals/ 无任何感知）。
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
