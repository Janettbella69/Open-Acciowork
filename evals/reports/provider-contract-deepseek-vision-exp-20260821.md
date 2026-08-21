# Provider 合约测试报告 · deepseek-vision-exp · 20260821

SDK: claude-agent-sdk 0.2.143 · 依据 PRD v2.1 §4.9 合约矩阵

| 用例 | 结果 | 说明 |
|---|---|---|
| basic_text_streaming | ✅ PASS | served=['deepseek-v4-flash-vision-exp']; streaming=yes |
| model_identity | ✅ PASS | served model prefix matches deepseek-v4-flash-vision-exp |
| file_tool_chain | ✅ PASS | note.txt=present |
| bash_execution | ✅ PASS |  |
| mcp_tool_call | ✅ PASS | tool_called=True |
| plugin_skill_discovery | ✅ PASS | discovered=18 skills |
| native_web_tools | ✅ PASS | native WebSearch served results |
| vision | ✅ PASS | model read the image and reported 3074 |
| error_mapping_bad_token | ⚠️ DEGRADED | invalid key hangs the SDK instead of failing fast; bounded by OPHIRA preflight timeout (FR-06 guard) |
