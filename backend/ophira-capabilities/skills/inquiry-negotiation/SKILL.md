---
name: inquiry-negotiation
description: 当用户要写询盘、比价、还价草稿、供应商沟通或谈判要点时使用。草稿不得直接发送。
version: 1.2.0
---

# Inquiry & Negotiation

先核对目标市场、MOQ、交期、认证与付款方式等项目约束，再起草询盘。

## 触发

询盘、RFQ、还价、比价、供应商沟通、谈判要点、PI 草稿。

## 不要做

- 不编造 MOQ、证书或已达成协议。
- 不把草稿经自动化通道发出。
- 不伪造联系人或已读回执。

## 方法选择

1. 交期/MOQ/认证等缺口会影响报价或承诺时用 AskUserQuestion 补问；其余缺项保留 unknown，先完成可用草稿。
2. 用 `mcp__ophira_supplier__draft_outbound` 生成草稿；发送必须经用户批准。
3. 报价对比走 `compare_supplier_quotes`；缺字段标 unknown。
4. 每一次对外草稿与依据证据写入账本。

## 按需资源

- 归一化报价、确定可谈项和承诺边界时读 [输入与证据核对](references/evidence-and-inputs.md)。
- 需要 RFQ 或还价草稿时，可用 [询盘与谈判底稿](templates/rfq-negotiation.md)，只选取与本次沟通相关的部分。
- 模板仅作新工作区产物的底稿；复制或改写到当前项目 workspace 后填写，保留待填项为 unknown，不修改只读 Plugin 包。内部联系记录不自动进入外发正文，发送须走已有授权路径。
