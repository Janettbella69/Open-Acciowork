# Green Path bundle · b-product-validation

- Run: `run_acc5834ec2dc41cfbffd7914d80a1287` · status **completed** · provider `deepseek`
- 判定：✅ PASS
- 成本（SDK Anthropic 牌价口径）：0.994941 USD · turns 17
- 实际服务模型：deepseek-v4-pro

## 工具覆盖

| 链路 | 观察到 |
|---|---|
| read | ✅ |
| web | ❌ |
| skill | ❌ |
| mcp | ✅ |
| bash | ✅ |
| write | ✅ |
| edit | ❌ |

## 治理断言

- 子代理事件：0（必须为 0）
- 权限请求 6 次；ask/allow/deny 闭环：✅
- deny 后继续收尾：❌
- 事件序号连续：✅
- 账本缺口：无
- 其他缺口：无
