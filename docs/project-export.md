# 项目数据导出

在已保存项目的工作区打开 **Project export / 导出项目**，先检查文件清单、大小和内容边界，
再选择 **Download reviewed ZIP / 下载已审阅的 ZIP**。下载交给浏览器处理，页面不把发起下载
当作已经保存到磁盘。更换项目或关闭面板会取消未完成的请求，并清除该面板的预览。

本地模式由本机操作者使用；可信网关模式要求当前项目的 owner/admin。member、reviewer、
viewer 和移动设备会话不能借此下载管理员范围的数据。清单绑定项目、操作者、租户、角色和
登录会话，下载前重新读取并核对。成员撤销、授权过期或内容变化后，旧清单不能继续下载。

## 包含内容

| ZIP 中的位置 | 内容 |
|---|---|
| `project.json` | 项目目标、证据、决策、产物登记及版本历史 |
| `workspace/`、`artifact-versions/` | 当前工作文件和已登记的不可变产物版本，保留原始字节 |
| `runs/` | 本应用可见的任务输入、消息、事件、运行记录、汇总及已保存任务经验上下文 |
| `receipts/` | 本项目运行的来源凭证和已净化 payload，读取时核验来源散列与运行身份 |
| `memory/` | 项目记忆及历史，删除墓碑对应的条目不会从历史中重新导出 |
| `access/`、`comments.json`、`shop-browsers/` | 项目成员、店铺 API 授权、讨论、店铺身份元数据；不包含登录配置目录 |
| `schedules/`、`task-experiences/`、`intents/`、`grants/` | 项目的计划、任务经验、持续意图和授权记录 |
| `notifications/`、`feedback/`、`commercial-actions/`、`channel-deliveries/` | 项目通知、反馈、商业动作记录和渠道投递记录 |
| `templates/` | 已保存项目模板及其版本、固定参考文件；不读取其他用户的私有模板创建请求 |
| `skill-proposals/` | 本项目的 Skill 候选记录、清单和冻结包，核验关联运行、产物和包散列 |
| `manifest.json` | 格式版本、项目和操作者、审核指纹、导出时间、逐文件字节数和 SHA-256 |

清单的文件数和总字节数计算项目数据，额外的 `manifest.json` 不计入自身清单。文件夹单列。
部分记录保留原部署的数据路径和来源散列以便追溯；它们不是 ZIP 内可直接访问的路径。
例如 `receipts/{project_id}/{receipt_id}/payload.json` 在 ZIP 中位于
`receipts/{receipt_id}/payload.json`。结构化记录脱敏后，以 `manifest.json` 的 SHA-256
核验导出文件；原来源散列表示脱敏前已验证的来源，不能用于校验变换后的 JSON 字节。

## 数据边界

结构化 JSON/JSONL 清理已识别的密钥、凭据及登录、SDK、浏览器会话标识，不截短业务正文，
保留数值 token 用量。原始工作文件、产物版本和 Skill 候选文件不做内容改写，因此也会保留
用户主动放入其中的私人信息。导出前的界面明确显示这个区别。

不读取凭据库、浏览器登录 profile、原生 SDK 转录、隐藏推理、可恢复的运行时会话、
共享 Agent/组织记忆、共享 Skills、设备注册或外部服务副本。也不包含工作区恢复检查点、
派生检索索引、共享 Agent 日记和操作者冷备份。导出不启动模型、任务、店铺浏览器或外部调用。

此 ZIP 用于留存和检查项目数据，不能恢复运行中的应用。完整应用迁移使用
[操作者冷备份](application-backup.md)。项目/会话删除、凭据的独立授权导出与删除、
恢复 UI 和生产灾备仍是[交付清单](prd-delivery-inventory.md)中的待交付项。

## 一致性与容量

- 必须等待该项目所有运行结束。捕获持有项目文件租约和账本锁；发现未完成的工作文件编辑、
  工作区恢复或任务经验恢复时停止导出，不通过导出接口修改或修复源数据。
- 下载重新捕获数据并比较指纹。新增、删除、修改文件或相关记录要求重新审核；其他项目的
  正常更新不会使当前清单失效。共享记录库扫描仍有容量上限，损坏记录会阻止核验。
- 只从明确的项目数据来源读取；拒绝路径逃逸、符号链接、硬链接、特殊文件、大小写冲突、
  不完整来源及散列不符。旧产物版本如果已经丢失原始字节，无法补造，整个导出会失败。
- 最多 10,000 个数据文件、30,000 个扫描/路径条目、128 MiB 未压缩项目数据，单文件最多
  64 MiB；结构化输入单文件最多 16 MiB。源读取总量也受限，包括共享记录扫描和最终复验。
  界面每页显示 50 项。ZIP 在内存中生成，大型部署容量和并发负载尚未认证。

## HTTP 接口

| 请求 | 响应 |
|---|---|
| `GET /api/projects/{project_id}/export` | `ophira-project-export-v1` 格式清单、指纹、数量、逐文件散列和边界说明 |
| `POST /api/projects/{project_id}/export/download` | 严格请求体 `{"fingerprint":"64位小写十六进制"}`；成功返回 ZIP 附件 |

所有成功和失败响应均使用 `Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。
请求体最多 1 KiB。权限变化返回 403，来源不存在返回 404，数据变化、忙碌或核验失败返回
409。错误不回显私有输入或内部存储路径；409 后重新检查数据并刷新清单。

## 验证

```sh
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests/test_project_export*.py -q
PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_project_export.py
```

第二条命令创建独立临时数据目录和真实 loopback HTTP 服务，验证上传、Shopify 身份、
清单变化拒绝、ZIP 字节和 SHA-256，结束后清理。卖家地址使用固定测试 DNS，不访问卖家账户。
浏览器验收及全量回归结果记录于[交付清单](prd-delivery-inventory.md)；本地验收不替代真实
Provider、Shopify 登录、远程设备或生产多租户验收。
