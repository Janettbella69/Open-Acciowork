---
name: compliance-claims
description: 当用户要核对外销宣称、认证标记、标签用语、CE/UKCA/FDA/CPSIA 等合规要点时使用。不提供法律意见。
version: 1.2.0
---

# Compliance Claims

用公开口径核对宣称。本 Skill 不代替律师或实验室。

## 触发

CE、UKCA、FDA、CPSIA、宣称、标签、认证标记、合规要点。

## 不要做

- 不得把 CE 写成质量标志。
- 没有对应产品和用途的正式批准依据时，不得宣称 FDA approved；注册、列名或检测报告不能代替批准。
- 不得把“待验证”改写成已通过。

## 方法选择

1. 可用 `mcp__ophira_compliance__lookup_requirements` 定位随包公开资料；涉及当前适用要求时核对原始官方来源，保留 source_url 与生效日期。
2. 不确定就写“待验证”，并列出需要用户或实验室补充的文件。
3. 输出宣称白名单/黑名单草稿，供用户法务审。

## 按需资源

- 核对产品适用范围、证书与宣称时读 [输入与证据核对](references/evidence-and-inputs.md)。
- 需要逐条审阅标签或广告用语时，可用 [宣称审阅表](templates/claim-review.md)。按任务选取字段，不要求每次完成整张表。
- 模板仅作新工作区产物的底稿；复制或改写到当前项目 workspace 后填写，保留待填项为 unknown，不修改只读 Plugin 包。模板与审阅结论均不授权外发或发布。
