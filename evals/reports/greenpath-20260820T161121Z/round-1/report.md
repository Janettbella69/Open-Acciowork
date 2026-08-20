# Green Path bundle · round-1

- Run: `run_891a39cba8bb47889883fb5ef8cdd79b` · status **completed** · provider `deepseek`
- 判定：❌ FAIL
- 成本（SDK Anthropic 牌价口径）：0.539852 USD · turns 14
- 实际服务模型：deepseek-v4-pro

## 工具覆盖

| 链路 | 观察到 |
|---|---|
| read | ✅ |
| web | ✅ |
| skill | ✅ |
| mcp | ✅ |
| bash | ✅ |
| write | ✅ |
| edit | ✅ |

## 治理断言

- 子代理事件：0（必须为 0）
- 权限请求 5 次；ask/allow/deny 闭环：✅
- deny 后继续收尾：❌
- 事件序号连续：✅
- 账本缺口：无
- 其他缺口：['permission:deny_then_continue_missing']
