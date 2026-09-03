---
name: inquiry-negotiation
description: 当用户要写询盘、比价、还价草稿、供应商沟通或谈判要点时使用。草稿不得直接发送。
version: 1.1.0
---

# Inquiry & Negotiation

先核对目标市场、MOQ、交期、认证与付款方式等项目约束，再起草询盘。

## 触发

询盘、RFQ、还价、比价、供应商沟通、谈判要点、PI 草稿。

## 不要做

- 不编造 MOQ、证书或已达成协议。
- 不把草稿经自动化通道发出。
- 不伪造联系人或已读回执。

## 步骤

1. 缺交期/MOQ/认证时用 AskUserQuestion，不要默认。
2. 用 `mcp__ophira_supplier__draft_outbound` 生成草稿；发送必须经用户批准。
3. 报价对比走 `compare_supplier_quotes`；缺字段标 unknown。
4. 每一次对外草稿与依据证据写入账本。
