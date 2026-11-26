---
name: office-documents
description: 当用户要导出或生成 XLSX、DOCX、PPTX 办公文件（报价表、方案、路演页）时使用。创建电子表格、Word 或幻灯片时优先用本 Skill。
version: 1.2.0
---

# Office Documents

在项目工作区内写出真实 OOXML 文件。不要用手写假扩展名或只在对话里贴表格冒充交付。

用沙箱里的 `python` 写：它已装好 openpyxl（XLSX）、python-docx（DOCX）和 python-pptx（PPTX）。脚本和产出的文件都放在工作区里，修订已有文件时在同一个文件上改。

## 质量门槛

- 合计、比率、差额必须写公式（单元格以 `=` 开头），不要把 Python 算好的死数字填进去。
- 中文内容不要依赖默认西文字体；交付前用 Read/Glob 确认文件存在且体积大于空壳。
- 数字必须来自已核对的资料或用户材料，并在正文或备注里留下来源。
- 办公文件不等于已发送的对外承诺。

## 不要做

- 不要为了“看起来完整”伪造销量表或认证页。
- 不要引入需要联网 `npm install` 的办公流水线；本运行时以沙箱内 Python 为准。

## 按需资源

- 选择格式、保留数据来源和检查公式/渲染时读 [输入与证据核对](references/evidence-and-inputs.md)。
- 需要报价 XLSX 时，可用 [报价工作簿底稿](templates/quotation-workbook-brief.md) 组织数据与公式；它是生成真实工作簿的可编辑底稿，不替代 XLSX 交付，也不要求其他文档任务照用。
- 模板仅作新工作区产物的底稿；复制或改写到当前项目 workspace 后填写，保留待填项为 unknown，不修改只读 Plugin 包。生成办公文件不等于可以发送。
