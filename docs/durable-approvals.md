# 持久审批与同 Run 续接（FR-13 / FR-25）

审批等待使用固定 Claude Agent SDK 0.2.143 的原生 `PreToolUse: defer`。
SDK 确认 `tool_deferred` 后，当前 CLI 退出，Run 保持 `waiting_permission`。
另一个同宿主控制进程可以读取审批、保存人的决议，并恢复同一 SDK session 的原工具调用。
应用不拼接虚假 tool_result、不发新业务 prompt，也不创建替代 Run。

## 调用与授权边界

- `GET /api/permissions/pending` 返回 durable/state/tool_use_id/expires_at。
  `preparing` 仅是请求；只有 SDK 确认暂停后才向用户展示可处理的 `pending`。
- `POST /api/runs/{run}/permissions/{request}` 在持有执行租约时保存决议并启动续接。
  SDK 尚未退出、其他 worker 已占用、重复提交、超时或配置失效返回 409。
- 决议已保存但进程在启动前退出时，状态为 `resolved`；
  `POST /api/runs/{run}/resume-permission` 继续处理已保存的决议，包括拒绝。
- 移动 companion 有对应接口。设备只可 `allow_once` 或 `deny`，不因持有设备 cookie
  获得跨项目、长期权限或后台管理身份。所有接口均复验当前授权。
- 绑定涵盖 Run、项目、SDK session、原 tool ID/name/input 指纹、principal、Provider
  与 Skill 快照。恢复和消费前重新检查当前成员及部署租户；恢复还复验 Provider
  白名单、启用/资格/凭据/预检状态、冻结配置、Skill 政策与剩余预算。
- 原生启动 command hook 经私有 Unix socket 调用同一宿主策略；它在 Python SDK
  initialize 注册回调之前已生效，防止恢复工具在注册回调前重放。失败返回明确 deny/stop，
  原生 ask 规则与 `can_use_tool` 一次性交接提供后续门禁。
- `AskUserQuestion` 使用原输入检查后，经 SDK permission callback 的 `updated_input`
  回填人选答案。SDK 实际发出的回答/错误事件被审计，不把预先批准当成工具执行成功。

## 恢复、资源与限制

每个 Run 的执行租约和记录锁共同约束进程竞争。批准先持久化；决议在返回 SDK allow/deny
之前持久标记为 `consumed`。消费前已确认的 checkpoint 可以恢复；消费后的进程异常属于
`uncertain`，不能自动重放。取消使审批失效；正常会话续接/重试不能绕开悬挂的 checkpoint。
这不承诺外部副作用 exactly-once，外部动作仍使用独立的 review/apply 和幂等收据。

批准等待默认八小时，可由 `OPHIRA_PERMISSION_TIMEOUT_SECONDS` 配置。过期记录保留可见，
不能继续批准或消费，可以取消。Run 的金额和可选 turn 上限跨 CLI 段累计，缺失用量保持未知
并拒绝自动续接；原始 `started_at` 不因恢复改写。Deep Research 冻结原始墙钟截止时间，
包括暂停期间的经过时间，恢复不能获得新时限。

固定 SDK 对并行工具批次的 defer 没有完整安全保证。因此只在主 Agent 完整
`message_stop` 边界证明恰好一个工具调用时持久暂停；多工具或无法证明的批次被拒绝，
提示主 Agent 拆成单次调用。子代理 stream 不能改变主 Agent 的批次判定。

跨宿主 session storage、Windows 桥接、宿主断电/磁盘损坏、远程文件系统锁，以及真实手机
HTTPS/后台运行仍需另外验收。当前原生探针使用两个独立控制平面对象和真实 CLI 子进程，
模型响应是本地编排 fixture；另外的 spawn 进程测试验证文件账本 resolve/consume 竞争。
它们不代表真实 Provider、生产多租户或手机锁屏验收。

## 验证

`backend/tests/test_durable_permissions.py`、`test_durable_permission_audit.py`、
`test_deferred_provider.py`、`test_native_hook_bridge.py` 与已有权限/会话回归覆盖本地合同。
真实 SDK 的离线探针和早期启动竞态证据见 [native-defer-contract.md](native-defer-contract.md)。

```sh
PYTHONPATH=backend/src .venv/bin/python backend/tests/native_defer_probe.py --integrated
PYTHONPATH=backend/src .venv/bin/python backend/tests/native_defer_probe.py --integrated --decision deny
PYTHONPATH=backend/src .venv/bin/python backend/tests/native_defer_probe.py --integrated --tool-kind write
PYTHONPATH=backend/src .venv/bin/python backend/tests/native_defer_probe.py --integrated --tool-kind clarify
```
