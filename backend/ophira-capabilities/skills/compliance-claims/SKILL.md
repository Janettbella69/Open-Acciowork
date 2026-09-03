---
name: compliance-claims
description: 当用户要核对外销宣称、认证标记、标签用语、CE/UKCA/FDA/CPSIA 等合规要点时使用。不提供法律意见。
version: 1.1.0
---

# Compliance Claims

用公开口径核对宣称。本 Skill 不代替律师或实验室。

## 触发

CE、UKCA、FDA、CPSIA、宣称、标签、认证标记、合规要点。

## 不要做

- 不得把 CE 写成质量标志。
- 不得在没有清单时宣称 FDA approved。
- 不得把“待验证”改写成已通过。

## 步骤

1. 用 `mcp__ophira_compliance__lookup_requirements` 查公开口径，保留 source_url 与生效日期。
2. 不确定就写“待验证”，并列出需要用户或实验室补充的文件。
3. 输出宣称白名单/黑名单草稿，供用户法务审。
