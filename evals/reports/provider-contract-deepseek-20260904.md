# Provider 合约测试报告 · deepseek · 20260904

SDK: claude-agent-sdk 0.2.143 · 依据 PRD v2.1 §4.9 合约矩阵

| 用例 | 结果 | 说明 |
|---|---|---|
| basic_text_streaming | ✅ PASS | served=['deepseek-v4-pro']; streaming=yes |
| model_identity | ✅ PASS | served model prefix matches deepseek-v4-pro |
| file_tool_chain | ✅ PASS | note.txt=present |
| bash_execution | ✅ PASS |  |
| mcp_tool_call | ✅ PASS | tool_called=True |
| plugin_skill_discovery | ✅ PASS | discovered=27 skills |
| native_web_tools | ✅ PASS | native WebSearch served results |
| vision | ⚠️ DEGRADED | no image comprehension; GUI ladder (FR-20) needs a vision-capable profile; read_tool_called=True; reply='VISION_UNAVAILABLE' |
| error_mapping_bad_token | ⚠️ DEGRADED | invalid key hangs the SDK instead of failing fast; bounded by OPHIRA preflight timeout (FR-06 guard) |
