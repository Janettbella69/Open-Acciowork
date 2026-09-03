---
name: landed-cost-analysis
description: 当用户需要计算到岸成本、毛利、盈亏平衡、现金占用或价格敏感性，并比较商业方案时使用。
version: 1.1.0
---

# Landed Cost Analysis

使用商业计算 MCP，不在自然语言中心算关键财务结果。关税/HS 细节走 `logistics-customs`。

## 触发

到岸成本、landed cost、毛利、盈亏平衡、现金占用、价格敏感性、DDP。

## 不要做

- 不得用单一毛利率替代现金流风险。
- 不得把估算税率写成 observed_fact。
- 不得口算合计；合计必须来自 MCP 或表格公式。

## 步骤

1. 把币种、税率、运费分摊、平台费、退款率、获客成本、MOQ 与付款周期列为显式输入。
2. 区分已知报价、用户假设和模型估算。
3. 输出基准、悲观、乐观情景以及最敏感的两个变量。
4. 将计算输入和结果写入可复算的 Markdown/CSV/JSON 产物。
