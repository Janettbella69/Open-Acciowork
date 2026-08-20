# Judge · 证据覆盖率（evidence-coverage）

> ⚠️ **未校准草稿**：本 judge 尚未经 gold 集校准（无 TPR/TNR 报告），输出只能作参考信号，
> **不得进入 §6.6 发布门禁**。校准流程见 `../gold/README.md`。

## 判定对象

一次运行的最终交付物（报告/简报正文）+ 该项目证据账本快照。

## 判定问题（逐条二元判定后汇总）

对交付物中每一条"关键事实性陈述"（数字、排名、供需结论、认证/合规要求）：

1. 账本中是否存在对应证据条目（claim 语义匹配即可，不要求逐字）？
2. 该证据是否带有效来源（source_url 或 source_path）与获取/观察时间？
3. 陈述强度是否与证据类型相称（community_signal 不得被表述为已证实销量）？

**输出 JSON**：`{"claims": [{"text": "...", "covered": true/false, "reason": "..."}], "coverage_rate": 0.0}`

## 边界口径（初稿，待 gold 标注中修订）

- 用户自述约束（预算、产能）引用为 user_statement 证据即算覆盖。
- 常识性陈述（"欧盟有 CE 要求"这类不带具体数值的）不计入分母。
- 模型明确标注"推断/假设"的句子不计入分母，由 fact-inference-confusion judge 单独看。
