---
name: office-documents
description: 当用户要导出或生成 XLSX、DOCX、PPTX 办公文件（报价表、方案、路演页）时使用。创建电子表格、Word 或幻灯片时优先用本 Skill。
version: 1.2.0
---

# Office Documents

在项目工作区内写出真实 OOXML 文件。不要用手写假扩展名或只在对话里贴表格冒充交付。

优先调用：

```bash
python -m ophira_backend.office_export xlsx workspace/quotes.xlsx --text "factory A,2.10" --text "factory B,1.95"
python -m ophira_backend.office_export docx workspace/brief.docx --title "市场结论" --text "证据见账本"
python -m ophira_backend.office_export pptx workspace/pitch.pptx --title "机会摘要" --text "下一步"
```

更复杂的表（多 sheet、图表、已有文件修订）可在沙箱里用 Python 标准库或已安装的 openpyxl 继续改同一个工作区文件。

## 质量门槛

- 合计、比率、差额必须写公式（单元格以 `=` 开头），不要把 Python 算好的死数字填进去。
- 中文内容不要依赖默认西文字体；交付前用 Read/Glob 确认文件存在且体积大于空壳。
- 数字必须来自账本或用户材料，并在正文或备注里留下来源。
- 生成后登记为 Artifact。办公文件不等于已发送的对外承诺。

## 不要做

- 不要为了“看起来完整”伪造销量表或认证页。
- 不要引入需要联网 `npm install` 的办公流水线；本运行时以沙箱内 Python 为准。

## 按需资源

- 选择格式、保留数据来源和检查公式/渲染时读 [输入与证据核对](references/evidence-and-inputs.md)。
- 需要报价 XLSX 时，可用 [报价工作簿底稿](templates/quotation-workbook-brief.md) 组织数据与公式；它是生成真实工作簿的可编辑底稿，不替代 XLSX 交付，也不要求其他文档任务照用。
- 模板仅作新工作区产物的底稿；复制或改写到当前项目 workspace 后填写，保留待填项为 unknown，不修改只读 Plugin 包。办公文件的生成与登记不授权发送。
