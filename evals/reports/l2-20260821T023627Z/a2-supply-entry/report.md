# Green Path bundle · a2-supply-entry

- Run: `run_229c862eaa0f4237bba856135f848ee0` · status **completed** · provider `deepseek`
- 判定：✅ PASS（Web 原生工具降级，见 FR-23）
- 成本（SDK Anthropic 牌价口径）：0.727152 USD · turns 12
- 实际服务模型：deepseek-v4-pro

## 工具覆盖

| 链路 | 观察到 |
|---|---|
| read | ✅ |
| web | ⚠️ degraded |
| skill | ✅ |
| mcp | ✅ |
| bash | ✅ |
| write | ✅ |
| edit | ❌ |

## 治理断言

- 子代理事件：0（必须为 0）
- 权限请求 4 次；ask/allow/deny 闭环：✅
- deny 后继续收尾：❌
- 事件序号连续：✅
- 账本缺口：无
- 其他缺口：无
