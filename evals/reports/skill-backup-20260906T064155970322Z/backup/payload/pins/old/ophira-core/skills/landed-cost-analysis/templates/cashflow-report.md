# {{项目 / 情景名称}}现金计划

币种：{{currency}} · 结算区间：{{start_date}}—{{end_date}}
输入指纹：{{input_sha256}} · 计算工具：ophira_commerce_math.cashflow_peak

{{一句话说明在所列现金计划与保留额下是否出现缺口；注明这是计算结果。}}

## 输入依据

| 收付款 ID | 结算日 | 收入/支出 | 金额 | 原始依据 | 事实/用户假设/推算 | 待核实事项 |
| --- | --- | --- | --- | --- | --- | --- |
| {{来自输入计划}} | | | | | | |

期初现金：{{opening_cash}}；最低保留现金：{{minimum_reserve}}。
{{注明现金范围、换算依据、遗漏费用、回款假设与来源未验证项。}}

## 余额与资金缺口

| 指标 | 工具结果 | 含义/时点 |
| --- | --- | --- |
| 期末余额 | {{totals.closing_cash}} | 区间内所有已列收付款后的余额 |
| 最低日末余额 | {{funding.minimum_closing_cash}} | {{funding.minimum_closing_cash_date}} |
| 满足期初/日末保留额的资金 | {{funding.funding_for_opening_and_daily_closes}} | 计算出的额外资金量 |
| 当天先付后收的保守资金量 | {{funding.funding_if_outflows_settle_first}} | 不等于已确认的日内结算顺序 |
| 首次缺口 | {{funding.first_shortfall_date / 无}} | {{funding.first_shortfall_phase}} |
| 期末缺口 | {{funding.ending_shortfall}} | 相对最低保留额 |

{{引用工具timeline输出；无收支日期余额沿用前日，展示省略不表示漏算。}}

## 情景差异与下一步

| 情景 | 具体输入变化 | 输入指纹 | 期末余额 | 资金需求 | 未验证假设 |
| --- | --- | --- | --- | --- | --- |
| {{只填写已实际调用工具的情景}} | | | | | |

{{列出需要补齐的条款/到账日期/证据和可由用户决定的下一步。不得写成自动融资或付款承诺。}}

复算附件：{{完整输入JSON、工具原始结果JSON、可编辑CSV或表格链接}}。
对账差额：{{totals.reconciliation_difference}}。
