执行 Deep Research 验收：以下均是合成 fixture、未验证的用户材料，不能当作真实商业报价。
仅使用 uploads/quote_en.md、uploads/quote_zh.md、uploads/repost_fr.md。不要搜索互联网、
使用 Bash、发消息、创建自动化、调用其他外部工具或修改输入文件。

在同一条调用消息中并行发起且仅发起一次三个原生 Agent：source-mapper, multilingual-researcher, conflict-verifier。
每次传 run_in_background=false，主 Agent 必须等三个返回后再综合。每个研究单元都要实际
Read 三个文件，分别着重映射原始来源、多语言重复来源、日期/价格/MOQ 冲突。
要求各单元遵循来源包协议：只返回 JSON，protocol="ophira-research-v1"、claims 数组、
unknowns 数组；每条 claim 有 claim/evidence_kind/source_locator/observed_at/excerpt/
language/conflicts/limitations。不写任何文件或账本，也不创建其他 Agent。

等三份结果返回后，主 Agent 亲自用 Read 重新核对三个原文件，然后：
1. 用 `mcp__ophira_ledger__record_evidence_batch` 一次提交两条原始报价的证据，各自包含价格、MOQ、
日期、短摘录、行定位，kind=user_statement，source_type=file，source_path 为对应 uploads
路径，source_title 自拟，confidence=0.5，supports_hypothesis_ids=[]，recorded_by=research-main。
在同一个 entries 数组末尾原样重复第一条，验证现有接口幂等返回相同 id。法语转载不得另建证据。
2. 用 `mcp__ophira_ledger__record_decision` 记录一个 plan：先澄清报价差异与缺失条件，暂不选择报价。
引用以上真实返回的两个不同 evidence_ids，说明价格/MOQ/日期差异，保留未知条件与后续动作。
3. Write deep-research-report.md，中文说明合成 fixture、未验证、两份报价数据、日期、冲突、去重依据、
三个研究单元的工作、未知条件，以及两个实际 evidence IDs 和决策 ID。
4. `mcp__ophira_ledger__register_artifact` 注册此文件，mime=text/markdown，source_ids 引用两个证据 ID。
不要伪造 source_receipt，不要把子 Agent 总结当成已经入账的事实。完成后简短确认。
