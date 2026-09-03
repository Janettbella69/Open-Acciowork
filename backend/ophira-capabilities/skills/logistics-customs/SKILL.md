---
name: logistics-customs
description: 当用户要查 HS 编码、关税、到岸成本里的税负、目的国进口限制或物流时效假设时使用。不代替报关行。
version: 1.0.0
---

# Logistics & Customs

跨境落地成本里的税与编码必须可核对。没有授权海关数据库时，用公开税则、用户材料和 `calculate_landed_cost`，并标明缺口。

## 触发

HS、税则、关税、duty、VAT、DDP/DAP、目的国限制、禁运、商检、到岸税负。

## 步骤

1. 写清原产国、目的国、产品描述；缺一项就用 AskUserQuestion 让用户选，不要默认。
2. 公开检索税则或用户提供的归类意见；把 HS 候选、税率、生效日记成 evidence，来源必须带 URL 或文件路径。
3. 把税率与运费、保险一并交给 `mcp__ophira_commerce_math__calculate_landed_cost`，不要口算。
4. 限制性品类、反倾销、认证前置条件单独列为风险，不确定就标待验证。

## 不要做

- 不要编造 HS 码或官方税率。
- 不要把 Chat 里的“大概 10%”写成 observed_fact。
- 不要声称已完成报关或已获得海关裁定。
