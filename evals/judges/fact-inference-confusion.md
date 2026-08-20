# Judge · 事实/推断混淆率（fact-inference-confusion）

> ⚠️ **未校准草稿**：未经 gold 集校准前只作参考信号，不得进入发布门禁。

## 判定对象

最终交付物正文 + 证据账本条目的 kind 字段。

## 判定问题

1. 正文中被陈述为既成事实（无限定词、无来源提示）的句子里，哪些实际只有推断/代理信号支撑？
2. 账本中 kind=inference / community_signal 的条目，正文引用时是否保留了其性质（"社区信号显示…"/"据此推断…"）？
3. observed_fact 条目是否真的对应可核验观察（文件内容、工具计算、带 URL 网页），而非模型断言？

**输出 JSON**：`{"confusions": [{"text": "...", "should_be": "inference|community_signal", "stated_as": "fact"}], "confusion_rate": 0.0}`
