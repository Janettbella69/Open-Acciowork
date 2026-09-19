# 产物与记忆提案事件

范围：PRD v2.2 §4.12、FR-14/16/17。账本及 MemoryProvider 是权威存储，
事件只观察已提交的记录，不重复业务写入，也不根据工具输出重建记录。

## 本轮要求清单

| 要求 | 状态 |
|---|---|
| 首次产物登记与后续不可变版本分别通知；Agent 显式登记时即可看到 | delivered：原生 SDK MCP handler 与 HTTP 回放已验证 |
| 直接记忆提案记录真实来源 Run；直接提案及运行结束蒸馏均可观察 | delivered：来源由宿主绑定；提案保持待审批 |
| 通知失败不改变业务结果；刷新、重启及并发恢复不重复通知 | delivered：提交前/后通知异常、写入进程退出、两个恢复进程竞争通过 |
| 事件只包含标识、版本和时间；不复制文件或记忆正文 | delivered：字段白名单与内容哨兵测试通过 |
| 工作台中英文提示、产物下载及记忆审核入口；SSE 回放恢复 | delivered：真实 loopback HTTP + Chromium，中英文320/400/1280px；运行中通知、两版下载内容、审核入口、刷新去重均通过 |
| 非 Run 人工变更、schedule/preflight 细分事件 | deferred：需要各自事件归属，不能伪造运行 |
| 真实模型质量、远程存储和生产负载验收 | deferred：本轮只验证隔离本地控制面与原生 MCP |

## 事件合同

- `artifact.registered`：首次保存的不可变 Artifact（version=1）。
- `artifact.updated`：后续版本（version>1），`artifact_id` 指向该次保存的固定版本，
  不随当前工作文件改变。相同字节的重复登记返回原记录，只观察原来源 Run。
- `memory.proposed`：来源为 `agent_proposal` 或 `distillation` 的记忆。
  仅在原 Run 对应的项目/Agent scope 和身份绑定一致时观察。没有可靠来源的旧提案跳过。
  已批准或拒绝的记忆仍可补发历史提案通知；正文已删除且未被观察的项不补造通知。

公共 payload 为 `schema_version=1/resource_id/recorded_at`，产物额外携带
`artifact_id/version`，记忆额外携带 `memory_id`。`recorded_at` 来自业务记录，
事件 timestamp/sequence 表示观察顺序；恢复不伪造原时间线。

去重以 Run、事件类型和资源 ID 为键，检查及追加共用原有跨进程事件锁；
没有第二份事件索引。历史完整 Artifact 通知可按其 `id` 识别，避免首次版本重复通知。
每次恢复都只读权威业务记录；事件错误不会使已完成的业务写入返回失败，
未完成的事件补发仍令完整流回放返回不可用或继续等待，不冒充尾部已完整送达。

工作台只用当前授权项目解析产物 ID，再构造下载链接，不信任事件中的 URL 或文件名。
记忆入口跳转现有设置页并重新读取当前授权记录，事件本身不批准记忆。
这不是人工编辑无 Run 产物的通知协议，也不改变 SDK 循环、工具权限或记忆审批规则。

## 验证

证据：[本轮报告](../evals/reports/resource-events-20260919/README.md)。
后端全量 **2148 passed / 13 skipped**，前端 **485 passed / 45 files**；
Ruff、ESLint、TypeScript/Vite、eval tools **59 passed**、Snapshot **61/61** 通过。
真实 HTTP 服务调用钉版 SDK 进程内 MCP handler，不调用模型；真实 Chromium 下载
比对两个文件版本的字节，并验证审核入口、运行中通知及刷新回放。进程退出后补发及
双进程去重由真实 spawn 测试覆盖。不能据此宣称真实模型、手机 HTTPS 或生产负载已验收。
