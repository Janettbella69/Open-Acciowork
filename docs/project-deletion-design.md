# 项目删除：实现范围与验收清单

状态（2026-09-19）：**项目删除已完成本地代码交付、全量回归、真实 HTTP 双进程中断恢复与中英文浏览器验收。独立会话删除及生产/历史备份治理仍 deferred。** 本文说明当前实现，不新增或替代架构权威；需求依据仍为 [PRD v2 §3.9、§4.10](OPHIRA-PRD-and-Technical-Architecture-v2.md)，架构以 [AGENTS.md](../AGENTS.md) 与 PRD §4 为准。

测试使用临时账本、合成记录和隔离的原生 SDK 会话，不使用真实用户项目、凭据或付费模型调用作为删除 fixture。下文路径只说明存储布局。

## 1. 范围与交付状态

| 要求 | 当前实现 | 验收状态 |
| --- | --- | --- |
| 完整项目删除 | 清理应用拥有的项目账本、工作文件、派生记录和可证明归属的原生 SDK 会话/检查点 | 已完成本地验证，证据见第 8 节 |
| 明确审核与确认 | 范围、计数、保留项、阻塞原因；准确项目名、非空理由与不可撤销确认 | 中英文真实浏览器及权限/迟到响应回归通过 |
| 删除后不可读写或恢复为原项目 | 项目外持久登记、跨进程共享/独占租约、存储与恢复入口门禁 | 已实现；旧版本进程必须停机升级，不能混跑 |
| 不损坏共享内容与其他项目 | 定向处理共享记录，保留共享 Skill、模板血缘凭证和独立副本 | 已实现；生产跨宿主恢复仍 deferred |
| 不确定响应与中断恢复 | 操作 ID 幂等、独立进度资源、同 ID 重试、启动先恢复删除 | 已实现；只有 deleted 才表示应用控制范围完成擦除 |
| 独立会话删除 | 尚无单独删除某个 session/resume 链的 API/UI | **deferred**，项目内会话清理不等于此能力 |
| 外部备份、下载、消息和动作 | 审核与结果页面披露保留边界 | 不由项目删除撤回或擦除，独立治理及生产验收 deferred |

主要实现：project_deletion.py / project_deletion_api.py、project_deletion_capture.py / project_deletion_purge.py、project_deletion_proofs.py、project_lifecycle.py、project_session_cleanup.py、project_deletion_sockets.py；前端为 ProjectDeletionPanel、ProjectDeletionProgress 与 useProjectDeletion。

## 2. 用户流程与 API

已保存项目的工作区提供折叠的“删除项目”和先导出入口。只有本地操作者或当前项目获授权的 owner/admin 可以取得审核并确认。工作台不凭客户端角色推断权限；服务端 403 时显示明确的中英文权限说明，不提供确认表单。确认时服务端再次授权并复核指纹。

| 方法与路径 | 契约 |
| --- | --- |
| GET /api/projects/{project_id}/deletion | schema_version=1、项目 ID/名称、fingerprint、domains、files/bytes/runs/sessions/memories/shops 计数、retained、blockers、can_delete |
| POST /api/projects/{project_id}/deletion | 客户端规范 UUID operation_id、fingerprint、准确 confirmation_name、非空且不超过 1000 字符的 reason；返回操作资源 |
| GET /api/project-deletions/{project_id}/{operation_id} | 独立于项目读取进度：deleting/blocked/deleted、时间、域清单、保留项、安全错误和 can_retry |
| POST /api/project-deletions/{project_id}/{operation_id}/retry | 无正文，继续原操作；不新建操作、不撤销意图、不扩大原审核范围 |

输入名称/理由或勾选确认均不会自行发请求。审核变化、活动任务或无法验证的范围会阻止提交，用户必须重新审核；关闭审核不删除，已提交操作不能通过关闭面板撤销。

- **403**：无权访问该删除资源，不等同于项目读取权限也丢失。清除审核详情，并对仍选中的对应项目复验实际读取权限。
- **404**：项目或该操作不存在，不能推断已经提交或完成擦除。
- **409**：新删除未通过提交前复核，或重试与当前工作冲突。保留原编号，刷新审核或查询原操作，不自动换编号提交。
- **503 / 网络结果不确定**：删除可能已经开始，包括意图/完成状态 rename 后 fsync 失败。先 GET 原 ID，查到操作才认定受理；查询仍为 404 时，可明确选择原 ID、原正文重发或重新审核，不能误报成功。

deleting 和 blocked 在 can_retry=true 时都提供“重试此删除操作”。其他 worker 仍持有独占租约时重试会被拒绝并重新查询；持有者退出后可用同 ID 继续，不必等待服务重启。deleted 的重复请求返回原结果。

进度 URL 仅保存 deletion_project、deletion_operation 等标识，不保存正文、名称或理由。操作查询/重试严格绑定提交时的身份种类，以及 authenticated principal 的 tenant、principal、role、**session_id**。原身份会话可刷新链接；另一管理员、同一用户新登录会话或角色变化均不能通过链接读取原操作，返回 403。当前不承诺跨登录会话迁移进度；后台启动恢复不依赖浏览器在线。

成功与错误响应使用 Cache-Control: no-store 和 X-Content-Type-Options: nosniff。reason 只持久化 SHA-256 摘要，**不保存理由原文**。登记仍含授权标识、相对路径/散列、会话标识、时间和来源关系；“无正文”不等于不含敏感元数据。

## 3. 应用拥有的删除范围

捕获器使用明确目录规则及解析后的项目/Run/计划关联。未知布局、损坏记录、冲突归属、路径或容量验证失败会阻塞，不跳过后声称完整擦除。擦除复核持久意图与原审核计划，缺失条目可幂等重试，新增或变化的未审核内容不会被静默扩大删除。

| 存储域 | 当前处理 |
| --- | --- |
| 项目主目录 | projects/{id}/：账本、workspace、uploads/OCR、产物版本、检查点、恢复日志与暂存内容 |
| Runs、对话与索引 | 所属 runs/{run}/ 的记录、summary、events、task-experience-context；全局 _manifest.jsonl 精确移除本项目记录 |
| 来源与商业动作 | receipts/{project}/、commercial-actions/{project}/；本地记录删除不撤销已执行的外部动作 |
| 自动化与消息 | 项目 schedules、intents、notifications、feedback、channel-deliveries；对应 task-experiences、事务恢复日志和已知暂存文件 |
| 权限与协作 | grants、项目成员/marketplace 授权、comments、频道绑定；定向移除 Companion 的本项目授权和推送关联，保留设备其他项目 |
| 项目记忆 | memory/project/{id}/ 的快照、历史、删除审计及暂存；项目删除登记保存在独立目录 |
| 项目模板 | 模板、版本引用、对应 application/暂存；最小来源凭证维持其他项目的独立产物和血缘一致性 |
| Skill 提案 | 项目候选 record、manifest、冻结 package；已发布共享 Skill 与发布凭证保留，无正文来源凭证替代被删候选的校验关系 |
| 应用浏览器 | 项目 L1 profile、shop-browser 记录与拥有的 profile/session/download 目录；活动 host/launch 阻塞，审核过的私有 Unix socket 定向清理 |
| 原生 SDK 内容 | 可证明归属的 transcript、子代理内容和原生检查点/侧存储，见第 4 节 |

共享 Agent 日记按已验证的 Run section 长度和散列擦除，保留其他 Run 与用户编辑，不删除整天日记。其他项目不因共用 Provider/Agent/Skill 被一并删除。符号链接只移除已证明归属的链接，不跟随外部目标；原生检查点硬链接必须全部在审核范围内，否则阻塞。

## 4. 原生 SDK 会话与未知归属

ProjectSessionCleanup 依据仓库钉版 claude-agent-sdk 0.2.143 的本地文档与安装源码，从所有 Run 的 provider/session/resume 关联和 transcript 的 cwd/session 身份捕获内容，同时核对其他项目引用。不只看最新 Run，不删除整个共享 config root。

- 范围位于应用 runtime-config/{provider}，含 environment 回退。识别项目 cwd 编码和长 cwd 原生目录形式，再验证内容身份；旧 cwd 迁移或同 session 多位置且无法证明归属时阻塞。
- 隔离 helper 设置明确配置根后调用 SDK 官方 delete_session；不修改 API worker 共享环境，不访问宿主默认 ~/.claude，不读取 vault 来定位，不调用模型。
- SDK 删除 transcript 后，有界清理并核验审核中的主/子代理文件、file-history、tasks、session-env、image-cache、uploads、session debug/todo 等。SDK 的 best-effort 返回值不等于完整擦除证据。
- 跨项目共享 session、未知 native sidecar 归属、未归属且非空的 history/plans/paste-cache/shell-snapshots 等内容、非法路径和损坏 JSON 都阻塞。UI 显示 native_session_scope_unproven 说明；不忽略真实阻塞或扩大到整个 Provider 目录。
- 原生清理先于 workspace 清理，以便重试仍可验证 cwd；失败保持项目不可用并继续原计划。

本轮包含真实安装包的无模型 CLI/合成检查点测试；不代表真实付费模型、全部历史 SDK 版本、远端 SessionStore 或生产跨宿主会话验收。独立 session 删除仍需另行界定 resume 链、证据与产物依赖，当前没有用户入口。

## 5. 持久登记、并发与升级

状态保存在项目树以外的 project-deletions/{project_id}/state.json，来源凭证为同目录 proofs.json。状态只有 deleting、blocked、deleted；操作 ID 不可替换，完成状态不可回退，原项目 ID 不可重新创建。

普通读取、写入、恢复和长时工作经 ProjectLifecycle.lease() 取得非阻塞共享租约并检查登记。删除在独占租约内复核、原子持久化意图；多项目依赖按 ID 排序获取，锁不能升级。线程和 asyncio task 身份区分重入归属，子任务不能继承父任务的删除特权。稳定锁 inode 保留在 .project-lifecycle-locks/，不随正文删除。

当前流程要求先停止运行任务、关闭店铺会话。共享租约、run worker 或 shop owner/launch 仍活动时阻塞，确认不会先提交意图再强行清理；Run terminal 标签不代表 OS worker 已退出。店铺 socket 定位与 no-follow 边界见第 9 节。

意图一旦提交，项目对正常访问不可用，失败保留 blocked，不能回滚为可用项目。API 启动在记忆等内容服务构造、自动恢复与调度/通知启动前恢复未完成删除。任务经验 journal/context/retire、Skill 全局恢复与发布、Companion 保存等迟到入口也参与门禁；具体存储仍保留原有事务锁。

**引入本版本门禁时，旧 API worker、scheduler、SDK worker 和 shop host 必须全部停机后统一升级重启，禁止新旧版本混跑。** 旧代码不读取新增登记，只升级 HTTP handler 无法防止旧恢复/回写重建目录。升级后，在线项目删除仍由新版本租约与活动 worker 检查协调，不以全局停机代替项目级隔离。

## 6. 共享保留、备份与外部副本

审核和结果页面披露以下保留范围，不用默认计数 0 暗示“没有保留副本”：

- Provider 配置、凭据、connectors、组织策略，用户级/Agent 级记忆及其他项目。
- 已发布共享 Skill、共享 runtime cache、发布凭证，以及其他项目独立复制的模板产物。
- 无项目正文的删除登记、Skill/模板来源凭证和稳定协调锁；全局必要的反重放保护。
- 已发送消息、已执行商业动作、用户下载 ZIP/JSON、管理员备份和外部会话存储。个人 Chrome/L2 浏览器不属于应用可任意清空的项目 profile。

应用冷备检查已理解删除后的 Skill/模板来源凭证，带删除登记的快照保留该状态。CLI 恢复仍从指定快照到**新目录**，不自动合并另一个运行目录的最新删除登记。现在必须提供已停机的最新数据目录以逐项核对项目与会话删除登记，或显式承认无法对账；登记不符时不发布恢复目录。删除前旧快照可能含正文，也没有后来产生的登记；该门禁不擦除旧备份，无法对账时不能阻止操作者明确选择恢复旧内容。

运维重新启用历史快照前仍须完成其他数据域与外部副本的独立治理。项目与会话删除登记的本地跨目录对账已接入；跨宿主可信登记交付、远端备份擦除和生产灾备演练继续 deferred。删除完成提示仅适用于当前应用控制且已审核的范围。

## 7. 前端状态与未发送内容

Workspace 持有操作，进度组件独立于当前项目。刷新从 URL 查询同 ID；切项目或关闭审核后，迟到响应不会覆盖其他项目。StrictMode 首次 effect 的微任务取消避免重复预览，提交/重试有单次进行中的请求保护。

取得明确操作资源后，discardProject 移除侧栏项目、关闭 SSE，清空项目/Run、事件、服务端对话与摘要、指标、来源材料和待审批，并阻止晚响应恢复项目。**未发送草稿与本地附件保留**，不同于用户主动“新任务”时清空附件。项目组件卸载也取消其导出/预览请求。

预览或提交 403/404 后复验对应项目读取权限；权威 GET 403/404 才清私有视图，GET 200 或暂时 5xx 保留重试上下文。进度 403/404 清私有审核/操作详情，保留不含正文的操作标识。只有 deleted 显示“应用控制的项目内容已删除”；deleting、blocked、查询失败或缺失均不报完成。

中英文覆盖域标签、保留项、活动任务/原生归属阻塞和 403/404/409/503。320/400/1280px 审核和结果页均完成真实浏览器验证，证据见第 8 节。

## 8. 验证证据与剩余边界

本轮以 ed213af 为交付前基线。[结构化验证记录](../evals/reports/project-deletion-20260919/verification.json)
与同目录截图只使用临时数据，不把离线 SDK、合成身份或本地 HTTP 冒充生产认证。

| 验证项 | 实测结果 |
| --- | --- |
| 后端全量 | backend 目录 `../.venv/bin/pytest -q tests`：**2109 passed / 13 skipped，117.48s**；付费/外部环境门控仍关闭 |
| 最后补充的 socket 服务集成 | `test_project_deletion_sockets.py`：**9 passed**，包括真实 store 路径、实际 AF_UNIX、服务审核/擦除与其他 socket 保留；新增集成用例晚于全量收集，未把两次数字相加 |
| 后台与恢复边界 | 15 项后台专项覆盖 channel/push 发送及取消清理、跨线程 atomic write、真实 memory SIGKILL 和 workspace 目录交换中断；原生清理 25 项覆盖安装 SDK、checkpoint rewind、长 cwd/共享引用与残留 |
| 前端全量 | frontend 目录 `npm run test:run`：**464 passed / 43 files**；ESLint、TypeScript、Vite build 通过；既有大于 500 kB bundle 提示保留 |
| 静态与评测 | Ruff、`git diff --check` 通过；eval tools **59 passed**，Snapshot **61/61** |
| 真实 HTTP 双进程 | [HTTP 报告](../evals/reports/project-deletion-20260919/http-verification.json)：意图与原生擦除后进程直接退出 71；另一进程拒绝读取/新 Run，重启完成应用擦除，同 ID 重试幂等，另一项目可用 |
| 真实浏览器 | [浏览器报告](../evals/reports/project-deletion-20260919/browser-verification.json)：EN/ZH、320/400/1280px 的审核与结果页无横向溢出；错误名称不能提交；真实删除后列表清除、未发送草稿和本地附件保留、刷新原进度恢复，无页面异常 |
| 权限与不确定响应 | API/组件回归覆盖 owner/admin/member、另主体/另 session、确认后内容变化409、首次/最终 rename 后 fsync 报错503与原 ID 查询；这些错误路径不冒充本轮浏览器实测 |
| 共享来源与备份 | purge/template/backup 专项证明发布 Skill/模板来源仍可校验、他项目内容保留、Agent 日记只删精确生成块、pairing/push 关联定向改写；发布源项目删除后真实冷备/新目录恢复通过 |
| 尚未完成 | 独立会话删除 API/UI；真实身份网关/卖家、历史 SDK/远端 SessionStore、跨宿主历史备份对账、外部消息/下载/备份治理和长期生产故障演练 |

浏览器插件发现列表为空，按排查结果使用隔离 Chromium 和本地真实 API。
未读取实际业务数据或凭据目录，没有模型调用；未移除原有 deploy/companion/。

## 9. 实现复核中的路径约束

浏览器 Unix socket 不位于项目目录。`ShopBrowserStore.socket_path` 的目录哈希输入是 **data root 下的 `shop-browsers` 目录**，不能误用 data root 自身；否则清理器会把错误位置的“不存在”视为完成。清理定位必须以真实 store 返回路径作独立回归断言，不能仅让 preview 与 erase 使用同一个推测公式互相验证。Socket 仅在项目独占、host/session/launch 空闲条件下按审核过的 owner、inode 与类型进行 no-follow unlink；不连接、不发 RPC，不移除共享父目录或其他店铺端点。
