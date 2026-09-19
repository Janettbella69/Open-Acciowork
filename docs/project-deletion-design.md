# 项目与会话删除：依赖清单与待实现设计

状态：**设计草案，未实现、未交付、未完成故障验收**。本文件记录 2026-09-19 的代码审计和实现建议；不是新的架构权威。需求依据为 [PRD v2 §3.9、§4.10](OPHIRA-PRD-and-Technical-Architecture-v2.md)，实现仍须遵守 [AGENTS.md](../AGENTS.md) 与 PRD §4。不得将本文件或现有记忆删除能力当成项目/会话删除已完成的证据。

审计只检查源码、测试、SDK 本地文档和已安装包；未读取真实 `backend/data`、凭据或用户会话。路径均为存储布局说明。

## 1. 需求与交付缺口

| 要求 | 本文结论 | 交付状态 |
| --- | --- | --- |
| 用户可删除项目、会话 | 需要覆盖账本、原生 SDK 历史、浏览器和派生副本，不能只删 `projects/{id}` | 未交付 |
| 删除前明确确认 | 展示实际范围、保留副本、阻塞任务，并绑定授权主体及快照；确认对象变化后重新审核 | 未交付 |
| 删除后不可读、不可写、不可重启复活 | 全局持久 tombstone、跨进程生命周期门禁、所有恢复入口参与 | 未交付 |
| 不损坏其他项目及共享资源 | 精确项目关联选择；共享 Skill、模板血缘、Agent 日记逐项处理 | 未交付 |
| 可验证擦除与故障恢复 | 删除意图先持久化，逐域幂等擦除，失败可恢复；所有必须域验证后才完成 | 未交付 |
| 记忆硬删除 | 已有 tombstone-first、正文/历史/镜像清理；跨进程事务缺陷正单独修复，不能直接视作项目级屏障 | 独立能力，不覆盖本需求 |
| 备份、已下载导出、外部发送内容 | 不能声称一并擦除；提供可理解的保留边界和单独治理流程 | 必须披露 |

项目删除与会话删除需要分别定义产品范围。一个 SDK session 可以被多个 Run resume；只删除某个 Run 不会清除原生历史，删除 session 则影响整个 resume 链。建议会话以 `(project_id, provider_profile_id, sdk_session_id)` 分组，无 SDK session 的排队 Run 单独处理；同时列出关联产物、证据、收据和 Skill 提案。跨项目 session 关联应拒绝并诊断，不扩大删除范围。

## 2. 必须检查与擦除的应用副本

以下是待实现 allowlist，不是执行脚本。发现未知目录、损坏记录或无法确认归属时，应返回具体阻塞项，不得跳过后宣称完成。全局 JSON 存储必须按解析后 `project_id` 与其他主键关联精确选择，不能凭文件名前缀猜测。

| 存储域 | 项目删除范围与证据入口 |
| --- | --- |
| 项目主目录 | `projects/{id}/` 中的账本、workspace、uploads/OCR、artifact-versions、project diary、checkpoints、restore journal、pending edits 和 workspace-stage；见 `storage.py`、`workspace_checkpoints.py` |
| Runs 与日志 | 项目所有 `runs/{run}/`，包括 run、summary、events、task-experience-context；全局 `_manifest.jsonl` 精确清理该项目索引；见 `storage.py`、`runs.py` |
| 收据与商业动作 | `receipts/{project}/`、`commercial-actions/{project}/`；先冻结外部动作与回写，保留最小删除审计的政策须明确；见 `source_receipts.py`、`commercial_actions.py` |
| 自动化与消息 | 项目 schedules、intents、notifications、feedback、channel-deliveries；包含待发正文、claims、重试和结果。schedule 对应的 task-experiences 与 `.transaction.json` 必须一起处理；见 `scheduler.py`、`intent_watcher.py`、`channel_delivery.py`、`task_experience.py` |
| 权限与协作 | 项目 grants、`auth/projects/{id}`、`auth/marketplaces/{id}.json`、comments；取消 Feishu 的项目绑定与 Companion pairing 中该项目授权，保留同一设备的其他项目；见 `auth.py`、`marketplace_access.py`、`project_comments.py`、`companion*.py`、`channel_identity.py` |
| 项目记忆 | `memory/project/{id}` 的 items、history 与内容镜像；删除记录移入独立的无正文项目 tombstone，不能随项目目录清掉唯一防复活记录；见 `memory.py` |
| 项目模板 | 项目保存的模板、版本引用文件、application requests/staging。先完成下文的跨项目 origin/application 协议；见 `project_templates.py`、`project_template_apply.py`、`project_template_backup.py` |
| Skill 提案 | 该项目 proposals 的 record、manifest、package 与来源 Run；已发布 proposal 不能直接删掉仍被全局 publication receipt 要求的唯一关联记录；见 `skill_proposals.py`、`application_backup.py` |
| 应用拥有的浏览器 | `browser-profiles/{project}/l1`；同项目 `shop-browsers/{shop}.json` 与 shop 目录下所有 profile generation、session、download staging。待 host 确认退出后清理它的确切临时 socket；见 `browser_tools.py`、`shop_browser.py`、`shop_browser_host.py`、`shop_browser_store.py` |
| 原生 SDK 持久内容 | 该项目每个 provider/config-root/cwd/session 的主/子代理 transcript 与文件检查点等副本；确切定位和完整性验证见第 3 节 |

删除操作生成的审核记录不得含 prompt、memory text、receipt body、工作区内容或原始删除理由中的秘密。现有 memory tombstone 保存理由、内容 hash 和来源 ID，称为“无正文”比“无敏感数据”准确；新项目审计应限制理由长度、脱敏并设访问/保留策略。

## 3. 原生 SDK 会话与 worker 边界

已核对本地 [sessions](claude-agent-sdk/sessions.md)、[session-storage](claude-agent-sdk/session-storage.md)、[Python reference](claude-agent-sdk/python.md) 与钉版 0.2.143 包：

- `providers.py:build_worker_env` 为同 Provider 的项目共用 `runtime-config/{profile.id}`；环境回退是 `runtime-config/environment`。不能删除整个 config root；不能为了推导路径调用会访问 vault 的 `build_worker_env`。
- `sdk_adapter.py` 使用项目 workspace 为 cwd，并开启 `enable_file_checkpointing=True`。原生 transcript 位于 worker `CLAUDE_CONFIG_DIR/projects/<encoded-cwd>`。长 cwd 的编码包含截断/查找规则，不能简单替换字符后递归删除。
- 已安装 SDK `delete_session(session_id, directory=...)` 校验 UUID，删除主 JSONL 与同 session 的子代理目录；`directory` 表示 cwd。SDK 配置根取调用进程的 `CLAUDE_CONFIG_DIR`，不是应用传给 worker 的 env。API worker 直接调用可能指向宿主 `~/.claude`；严禁临时修改共享进程环境。建议由隔离 helper 以明确 config root/cwd 调用官方 API，并检查删除结果；不得改 SDK 内部实现。
- `delete_session` 的子目录清理使用 `ignore_errors=True`，返回成功不足以证明完全擦除；还需有界、无 symlink 跟随的残留验证。该 API 未清理所有文件检查点/CLI 缓存。文件检查点确切存储路径和关联规则仍是**必须实测确认的交付门槛**，不能把猜测路径写成删除规则。
- 原生会话存储与文件检查点是独立层；官方[文件检查点说明](https://code.claude.com/docs/en/agent-sdk/file-checkpointing)也明确 rewind 不改变会话。若以后接入 SessionStore，删除协议必须覆盖远端镜像及全部 subkeys；当前代码未配置 SessionStore。
- `runs.py:_resolve_resume_source` 已检查同 session 的项目、principal、agent、provider 和终态。删除必须从所有 Run 的可靠来源构建 session 集，不以单个最新 Run 作为唯一记录。
- 等待 runtime 的 `AsyncExitStack` 完成 SDK client、原生 hook bridge 和工具任务清理；仅 Run 标记 terminal 不代表 OS worker 已退出。`run-leases/{id}.lock` 在 run.json 写出前即可持有。

测试需覆盖相同 Provider 的两个项目、同项目多个 Provider、environment 回退、长 cwd、多个 resume Run、子代理 transcript、文件检查点和旧配置根迁移。未知历史来源应阻止“全部会话内容已删除”的承诺。

## 4. 共享副本：保留、定向擦除与披露

| 共享域 | 安全处理建议 |
| --- | --- |
| Agent 日记 | `storage.py:diary_dir` 将有 agent_id 的 Run 写入 `agents/{agent}/diary/YYYY-MM-DD.md`。按确认的 Run section 精确擦除 prompt/note，保留其他 Run 及用户编辑；旧 Markdown 结构的可靠解析须先测试。不可删整天日记 |
| Agent/User scope 记忆 | 即便 source_run_id 指向该项目，也可能是用户批准的共享事实。默认不静默删除全部共享记忆，审核页列出关联副本并提供独立授权操作。自动 distill 当前创建 PROJECT scope，属于必须擦除范围 |
| 已发布 Skill 与 runtime cache | 已安装版本可能被其他项目使用；保留共享 artifact/version，披露内容来源。`application_backup.py` 校验 registry publication receipt 与 proposal 对应，需加入内容已擦除的最小 origin/receipt tombstone，使备份验证仍成立。不得直接删共享 `skills/runtime/<snapshot-hash>` |
| 衍生模板项目 | target artifact 是独立副本，不能随 source 项目删除。`project_template_backup.py` 当前要求双向 origin/application 和 source/target 均存在，需 tombstone-aware 血缘与终态规则。`preparing/ready` application 可重建固定 target，必须永久拒绝已删除 target；`applied` target 缺失目前会报错，不能把错误当完整治理 |
| Companion/频道防重放 | 只移除 pairing 的该项目关联；全局 replay guard 保留必要反重放保护。已经发送到外部频道的内容、外部已执行商业动作需要单独撤回/取消流程，删除本地记录不代表撤销 |
| 用户拥有的资源 | Provider profiles、secrets、connectors、org policy、个人 Chrome/L2 profile 等不属项目；不得清空或关闭用户 Browser/Context。应用只释放自己拥有的 page/连接 |
| 备份与下载 | 管理员备份、已下载 ZIP/JSON、外部对象存储/日志的保留政策单独说明。恢复旧备份必须携带或对照最新删除登记；不能通过旧快照覆盖登记使项目复活 |

模板与 publication receipt 的跨域协议调整是项目删除的前置实现，不是可以上线后再补的清理工作。

## 5. 全局删除登记与恢复顺序建议

建议在不属于任何项目子树的受保护目录保存删除 intent/tombstone，包含不可复用 project ID、操作 ID、授权主体、快照摘要、范围版本、进度、已擦除域与保留说明；不保存待删正文副本。稳定状态可为 `deleting` / `blocked` / `deleted`，具体 schema 尚未确定。

1. 授权与 preview 本身只读。确认后在独占生命周期锁内复核权限、revision、共享关联和活动任务，持久化删除 intent 并 fsync 文件及父目录。此后项目 ID 永久禁止新建/恢复、读取和写入；失败也不能回滚成可用项目。
2. 所有参与者先看到全局屏障，再冻结自动化、权限回写、outbox、浏览器启动、模板申请和 Run 创建。安全停止在执行的任务；无法确认停止则保留 `blocked`，不得删除活跃锁文件或先毁掉无法恢复的关联信息。
3. 关闭已拥有的 worker/浏览器，排空事务。按 allowlist 幂等擦除正文，处理共享引用后记录每域完成；任何失败保留可重试进度。
4. 用相同来源规则重新扫描所有必须域与全局派生记录，确认无可读正文与可恢复 intent，再标记 `deleted`。仅请求已受理/屏障已建立不能向用户报告全部擦除完成。
5. 启动时在任何会读写项目的服务构造、自动恢复、scheduler/intent watcher/channel delivery/companion push 启动前加载删除登记并完成恢复。`FileMemoryProvider` 的构造本身即恢复；不能只在 lifespan 最后加清理。
6. `task_experience._recover` 能从 `.transaction.json` 写回 state 与 run context；shop host `finally` 能重建 session.json；channel delivery 对已有正文的 due item 可直接发送。上述恢复/回写都必须在共享门禁内重新检查删除状态。
7. 备份导入先合并不可撤销删除登记，再恢复其余记录；不得以更旧的登记覆盖新状态。项目 ID 不复用，模板幂等键/固定 target ID 同样受屏障约束。

可复用 memory 的“tombstone 先于正文清理，恢复重复清理”原则；不能直接复用其项目内存储位置。记忆旧实现的仅实例 RLock 允许其他实例覆写快照、历史与 MEMORY.md；修复后的跨进程 scope 事务仍只覆盖记忆，不能代替全项目生命周期门禁。

## 6. 在线锁模型与现有锁冲突

建议增加跨进程**项目生命周期共享/独占锁**：正常项目读写/恢复取得共享租约后检查 tombstone；删除取得独占租约并建立屏障。长时 worker 的终止协调应分为先登记禁止新工作、请求退出、再取得可完成擦除的独占权，避免删除永远等一个不会自行退出的共享持有者。

现有 `.ledger-locks` 保护短时 JSON 写且不能跨 await；workspace lease 保护工作区并发；run lease 标识执行；schedule dispatch lease 跨 await；API lifespan 与 shop host 的 maintenance shared/session/launch leases 具有进程生命周期。它们各自都不是全项目锁。API 已持有 maintenance shared 时直接请求 exclusive 会自冲突，不能用于在线删除。远 worker 仍活跃而无法取消时须明确阻塞。商业动作 inspect 在项目事务外、commit 在事务内，必须阻止 inspect 晚返回后重新提交，并等待已经进入 commit 的动作结束。

建议统一顺序为全局 maintenance → project lifecycle（多项目按 ID 排序）→ 具体 store/scope/dispatch/workspace 锁；**落地前必须审计所有已有相反顺序**，尤其由持有 schedule/workspace 锁的调用再进入 ledger 的路径，不能机械嵌套新锁造成死锁。生命周期锁需可重入语义与统一所有权，线程 RLock 不能冒充跨 worker 锁。长时等待不能持有阻塞 event loop 的同步 flock。

锁文件须位于不会随删除移除的协调目录，打开时拒绝 symlink/多 hardlink，并验证 regular file。不能删除活跃 inode 后重建同名锁，否则两个 worker 锁住不同 inode。应用级 `_atomic_write` 自动 mkdir，会让漏掉门禁的旧 writer 重建目录；仅在 API handler 检查一次 tombstone 不够。

全局维护独占锁可以作为明确停机的临时运维限制，但不能假称提供了在线项目删除。必须保留跨项目正常运行的验收。

## 7. 建议 API 与 UI 审核契约

本节为契约建议，当前不存在可用的项目删除接口。

- Preview 返回项目名称/ID、当前授权主体、revision、精确域计数、session 分组、运行/host 阻塞项、共享副本处理方案、备份/外部副本限制与 fingerprint。fingerprint 绑定项目自身记录、所选全局成员和共享依赖，避免无关项目变化导致无意义失效。
- 确认请求带一次操作 ID、preview fingerprint、目标 ID/名称复核与明确用户确认。owner/admin 权限需在执行时再次检查；权限变化、内容变化、新 session/new proposal 或关联变化返回需重新审核，不能自动扩大用户批准范围。
- 响应区分已受理、等待停止、擦除中、可重试失败、完成；以无正文 operation resource 查询进度。幂等重试同一操作不会恢复资源或重复外部副作用。
- UI 解释不可撤销范围并提供先导出入口；显示“应用控制的内容已删除”及实际保留项，不宣称撤回外部消息或擦除备份。取消审核不执行删除；已提交删除不能用关弹窗假装取消。
- 客户端项目缓存、会话正文、草稿、附件、导出 Blob、SSE 订阅与 pending approvals 同步清理；403/404/删除通知使旧响应失效，迟到网络结果不能恢复已删项目。后台拒绝继续 resume。
- 全部私有 preview/进度/错误/下载响应 `Cache-Control: no-store`；日志只记录操作和安全错误码。未授权者不能利用删除状态判断他人项目存在。

## 8. 必须通过的真实故障验收矩阵

不以单线程 mock 测试代替多 worker、真实子进程、文件系统和重启验收；所有 fixture 在临时目录，禁止用真实用户 data 或凭据。本文尚未运行这些项目删除验收。

| 场景 | 故障注入/竞争 | 必须结果 |
| --- | --- | --- |
| 多 worker 写入 | preview 后另一进程启动 Run、上传、批准权限、评论、导入模板 | 确认失效或新操作被屏障拒绝；无丢失其他项目数据 |
| SDK 仍执行 | Run 已 terminal，但 lease/子进程未退出；有 resume/subagent | 等待真实退出；日志/检查点擦除后不能 resume；共享 provider 的项目 B 原样 |
| 每个删除阶段断电 | intent fsync 前后、各目录擦除间、共享引用转换间、完成标记前 `SIGKILL` | 重启先恢复删除；不启动通知/自动化；可重复重试并最终验证 |
| task experience journal | 留下可恢复 transaction，删 run 后重启 | 不重建 run/context/experience |
| 延迟频道发送 | outbox 已有结果正文且正在 claim/reply | 不发出尚未发送内容；在途外部结果如实报告，不伪称撤回 |
| 浏览器 | 独立 host、download、session finally 回写、旧 profile generation | host/socket 已关闭；不复活 session，不残留应用 profile；L2 用户浏览器保持 |
| 共享内容 | 两项目共用 Agent/Provider/Skill；模板 source/target 互相删除 | 精确擦除项目日记；其他项目正常；backup/import 验证通过；血缘 tombstone 无正文 |
| 记忆 | delete 与 correct/rollback/expiry/recovery 并发，删除后恢复旧历史 | items/history/MEMORY.md 无被删正文；tombstone 不消失；其他记忆不丢失 |
| 备份恢复 | 导入删除前备份、删除中备份、损坏/更旧登记 | 不复活项目；明确拒绝无法安全对账的恢复 |
| 恶意路径 | symlink/hardlink、长 cwd、损坏 JSON、重复/外国 project ID、未知 config root | fail closed；不触碰其他项目、宿主配置或 secrets |
| 并发锁 | 两个删除进程、scheduler 持 lease、锁文件重建尝试 | 单一删除操作，无死锁/双 inode；失败可以恢复 |
| 前端 | 确认后撤权、切项目、关闭面板、SSE/下载晚返回、320/400px | 不泄露旧正文、不误报完成；确认可访问且计数/保留范围可见 |

实际交付时应在本清单逐条附对应测试/真实流程证据；未经验证项继续标记未交付，不通过减少删除范围改变 PRD 要求。
