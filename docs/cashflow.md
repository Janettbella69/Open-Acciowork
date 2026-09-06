# 确定性现金流与峰值资金缺口（FR-11）

`mcp__ophira_commerce_math__cashflow_peak` 使用调用者提供的结算计划计算余额和资金缺口。
它复用现有商业计算 MCP Server，列入领域工具及只读白名单；不访问外部服务、不写账本、
不改变权限，也不预测销量、回款概率、汇率、利率或资金可获得性。

## 输入与单位

| 字段 | 契约 |
| --- | --- |
| `currency` | 一个大写三字母单位标签；不检查货币目录，不自动换汇 |
| `start_date` / `end_date` | `YYYY-MM-DD`，含首尾共 1–3,660 天；不接收时间戳或时间部分 |
| `opening_cash` | 首日所有收付款之前的现金，可为负数 |
| `minimum_reserve` | 非负最低保留现金，默认 0；只是余额阈值，不列入支出 |
| `movements` | 最多 500 条，允许空列表；不会补出遗漏的收付款 |

每条 movement 有唯一 `id`（最多 80 字符）、区间内结算 `date`、`direction`
（`inflow` / `outflow`）、正 `amount`、`activity`（`operating` / `investing` /
`financing`）。可选 `description` 最多 200 字符，`source_ref` 最多 300 字符。
分类和引用均由调用者提供；引用不是已验证 Evidence，也不会签发或验证来源收据。

每个输入金额绝对值不超过 1,000,000,000,000 个声明币种单位，最多四位有效小数。
接受 JSON 数字或普通十进制字符串，拒绝布尔、非有限值、科学计数字符串和需要舍入的金额。
计算采用 Decimal；所有结果金额用**固定四位小数的十进制字符串**返回，避免 JSON 浮点误差。
这表示规划精度，不声明该币种现金支付的最小面额。

## 结果和会计关系

`totals` 提供收款、付款、净变化、期末现金和三类活动的分项。始终满足：

```text
期末现金 = 期初现金 + 已列收款 - 已列付款
reconciliation_difference = 0.0000
```

已列融资到账/还款按输入方向计入现金一次；不会自动增加资金、偿还贷款或计提利息。
收入确认、利润、应收款和结算现金是不同口径，本工具只计算最后一种。

`funding` 中：

- `opening_shortfall` 保留首日期初缺口，即使首日稍后有收款。
- `peak_closing_shortfall` 只衡量各日期的日末余额与保留额之差。
- `funding_for_opening_and_daily_closes` 是覆盖期初及所有日末缺口所需的额外资金峰值。
- `funding_if_outflows_settle_first` 另计同日先付款、后收款的保守日内缺口。模型不知道实际
  日内顺序，因此此值不能称作已经发生的现金缺口。
- 同时返回最低日末余额/日期、首次缺口日期/阶段、保守峰值日期/阶段和期末缺口。

资金需求为满足所声明保留额的**额外**资金，不把已有期初现金或已列融资再算一次。
等额增加期初现金可以覆盖对应模型的缺口，但该计算不表示资金已经到账。

`timeline` 包含首日、每个交易日及末日，最多 502 行。同日输入顺序不影响结果；无变化日期
省略，余额延续至下一行。`timeline_coverage` 明示行数、日期覆盖及省略规则。

## 来源、文本与大结果

输出明确列出缺少来源引用的条数、`references_verified: false` 和模型假设。
未输入的税费、退款、利息等不在计算中，不意味着实际没有这些成本。

MCP 仅对 `description` / `source_ref` 使用现有不可信文本清洗，并附带统一数据说明。
不把整份财务结果交给文本截断器，防止金额或日期被截成另一数值。`input_text_sanitized`
标记正文是否改变；`inputs` 保留规范化后的完整输入，`input_sha256` 对应这些返回输入，
按日期/ID 排序后可复算。来源引用始终只是未验证标签。

完整 MCP JSON 保留所有结果行。大结果遵循 SDK 自有的文件/预览机制，不增加全局上下文
预算。事件发布器仍对单个文本字符串使用 20,000 字符上限并附加 `…[TRUNCATED]`；输出将
摘要放在前部，保证边界内先看到余额和缺口。事件中的截断预览不是完整 JSON，也不应充当
完整计算产物；工具输入在 `tool.started` 中以结构化字段保留，可再次计算，完整 MCP 结果
可由主 Agent 保存为项目产物。

## 本地验证

```sh
PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_cashflow.py -q
.venv/bin/ruff check backend/src/ophira_backend/cashflow.py backend/src/ophira_backend/ledger_tools.py backend/src/ophira_backend/constants.py backend/tests/test_cashflow.py
```

测试覆盖结算时点、日末/保守日内峰值、负期初现金、reserve 不重复扣账、融资分项、
Decimal 精度、完整区间及容量边界、非法输入、真实 SDK in-process MCP schema/错误结果、
来源注入清洗与指纹、502 行最大结果和明确的事件预览截断。测试数字是隔离计算用例，
不是真实市场数据；这些检查也不证明任何业务输入或未来收款可靠。
