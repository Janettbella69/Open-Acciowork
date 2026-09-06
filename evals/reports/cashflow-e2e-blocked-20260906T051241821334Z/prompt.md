请完成一个明确标为合成 fixture 的现金流验收任务，不代表真实业务、报价或回款。
使用 Plugin Skill `ophira-core:landed-cost-analysis`。Skill 加载后，用真实 Read 完整读取该 Skill 自己目录中的
references/cashflow-inputs.md 和 templates/cashflow-report.md，不要只凭摘要或猜测内容。
再用 Read 读取工作区 uploads/cashflow-fixture.json，将其中全部显式输入原样用于
`mcp__ophira_commerce_math__cashflow_peak`，此计算工具只调用一次，不增补假设或其他收付款。

用 Write 将该计算工具返回的完整 JSON 对象原样保存为工作区根目录 cashflow-result.json，不是摘要，
不要省略 inputs、timeline、note 或其他字段。再依据已读取模板用 Write 写 cashflow-report.md。
Markdown 必须说明这是“合成 fixture”、来源“未验证”、reserve 不列支出、先付后收仅是保守假设。
报告末尾增加以下审计表（值来自真实工具结果，保留四位小数，无单位后缀）：
| 指标 | 工具值 |
| --- | --- |
| funding_for_opening_and_daily_closes | 对应 funding 字段 |
| funding_if_outflows_settle_first | 对应 funding 字段 |
| closing_cash | 对应 totals 字段 |
| input_sha256 | 完整输入指纹 |

最后分别调用 `mcp__ophira_ledger__register_artifact` 注册上述两个文件，source_ids=[]，
mime 分别 application/json 和 text/markdown。不要调用 Bash、外部搜索、外发、调度或子 Agent，
不要修改 Skill 原件。其他不相关工作无需执行，完成后简短确认。
