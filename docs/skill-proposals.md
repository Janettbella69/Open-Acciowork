# Skill 自创建提案与人审发布

PRD v2 [§3.7 / §4.7](OPHIRA-PRD-and-Technical-Architecture-v2.md) 的提案路径由
主 Agent 编写候选包、宿主冻结与扫描、用户审阅发布组成。候选始终先进入 `pending`；
扫描通过后仍须人审，发布成功后默认禁用，用户另行启用才会影响后续运行的 Skill 选择。

该流程沿用 Claude Agent SDK 的原生 Write/Edit、现有 `register_artifact` 和
托管 Plugin。没有新增 MCP 工具、模型循环或固定业务 Agent。Skills 的发现和运行
继续遵循本地 SDK [Skills](claude-agent-sdk/skills.md) 与
[Plugins](claude-agent-sdk/plugins.md) 文档；工作区候选不加入运行时 Plugin。

## 用户路径

1. 用户描述可复用的流程，或主 Agent 在完成任务时发现值得沉淀的方法。Agent 说明适用
   场景、方法、限制及来源，按需准备提案。没有按 Run 数量、重复次数或业务关键词自动
   判定“值得沉淀”的宿主规则；`reason` 是 Agent 的说明，由人判断其价值。
2. 主 Agent 用原生 Write/Edit 在项目工作区写入下面的固定目录，再显式注册 manifest。
   普通文件注册及运行结束的自动 Artifact 扫描不会提交 Skill 提案。
3. 宿主核对实际 Run、项目与 Artifact 来源，冻结 manifest 和整个包，生成文件清单与
   当前扫描结果。候选出现在 Plugins → Skills → **Proposals**；初始筛选为待审。
4. 有权限的用户查看冻结的 `SKILL.md`、references/templates、扫描、内容哈希、相关
   Run 摘要及目标版本。文件按普通文本展示；二进制及部分预览可下载冻结原件。
   界面要求完整加载 `SKILL.md` 并明确确认审阅后才能发布；这不代表宿主能够证明
   审阅人已实际阅读每个资源。
5. 批准会再次扫描并比较提交时携带的目标令牌；拒绝必须填写原因，危险候选也可以拒绝。
   已发布 Skill 在 **Installed** 中管理，仍需单独启用。拒绝记录、冻结包和审阅历史保留。
   发生冲突或结果不明时，先刷新已记录状态，再重新审阅。

## 编写与显式提交

工作区目录必须是：

```text
skill-proposals/
└── supplier-checklist/
    ├── manifest.json
    └── package/
        ├── SKILL.md
        ├── references/evidence.md
        └── templates/report.md
```

`manifest.json` 示例：

```json
{
  "schema_version": 1,
  "kind": "ophira.skill-proposal",
  "name": "supplier-checklist",
  "version": "1.0.0",
  "package_dir": "package",
  "reason": "用户描述了需要反复执行的供应商核对流程。该方法整理证据与缺口，不将未核实报价写成事实。",
  "related_run_ids": []
}
```

`package/SKILL.md` 示例：

```markdown
---
name: supplier-checklist
description: 按用户给定的供应商资料整理已核实信息、证据缺口与待确认事项。
version: 1.0.0
---

阅读 references/evidence.md，区分事实、用户陈述与假设。
使用 templates/report.md 整理结果；缺失信息保持未知，不推断为已核实事实。
```

manifest 拒绝重复 JSON 键和额外字段。`schema_version` 必须是整数 `1`，不接受
`true`、`1.0` 或字符串。`name` 匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，必须与目录及
SKILL frontmatter 一致；`version` 是 1–100 字符的非空单行显式值，不能有首尾空白，
必须与 SKILL frontmatter 一致。没有强制 SemVer 排序规则。`package_dir` 固定为
`package`，`reason` 为 1–4,000 字符且不能全为空白。

`related_run_ids` 可省略，默认为空；最多 20 个且不能重复。填写时，宿主逐个核对它们
是同项目中已开始执行、已经终态的其他 Run，不能填生成提案的当前 Run。
`read_project_context.skill_proposal_context.related_runs` 提供最多 20 条最近符合条件
的真实摘要供按需选择。当前生成 Run 和 Artifact 始终由宿主绑定；历史 Run 数为零
不会阻止提案，也不表示宿主断言该流程已经重复执行过。

完成所有文件后，主 Agent 调用现有 `mcp__ophira_ledger__register_artifact`：

```json
{
  "path": "skill-proposals/supplier-checklist/manifest.json",
  "source_ids": [],
  "mime": "application/json"
}
```

`source_ids` 使用现有 Artifact 契约，只能引用同项目的真实 Evidence 或 Artifact ID，
与 manifest 中的历史 Run ID 分开。生产闭包绑定真实 Run；服务要求它具有
`started_at` 和已观测的 `sdk_session_id`。只读支线 Agent 不能 Write/Edit 或调用
注册 Artifact 等写账工具。响应除 Artifact 原字段外，包含 `skill_proposal` 的
`id`、`state`、`skill_id`、`content_hash`。提案的 Skill 命名空间为
`ophira-tenant:<name>`。

Artifact 先登记，随后才冻结提案；冻结失败时可能已有 Artifact 记录，不能把它当成
候选已成功提交。同一来源 Artifact 已有候选后，重复提交始终返回第一次冻结的候选，
不重新读取已改动或已删除的工作区。另一 Run 复用旧 Artifact 只能得到原归属候选；
若原 Artifact 没有候选，则不能代它创建。要提交修改，应更新包和显式版本化 manifest，
形成新的 Artifact；仅修改 package 而沿用相同 manifest 不会替换旧候选。

## 冻结、扫描与容量

候选保存在 `data_dir/skill-proposals/<proposal_id>/`，与 Registry 分开。宿主用目录
文件描述符逐层读取，不跟随路径组件链接；拒绝符号链接、硬链接和特殊文件。
非阻塞打开后再检查文件类型，FIFO 不会阻塞捕获。提交核对登记的 manifest SHA-256，
重复捕获比较源字节，写入私有 staging 后再次核对冻结内容、manifest 与扫描的包哈希，
最后原子发布候选目录。包文件和目录保持只读；这不是对拥有宿主操作系统权限者的防篡改保证。

冻结候选不出现在 Registry 的可发现列表、运行快照或历史中。人审发布事务会归档
经过审阅的包；中断事务可能留下历史包，所以“历史包存在”不能用来推断发布成功。
文件预览和下载重新核对 manifest、完整文件清单、各文件哈希和包哈希，不读取工作区副本。

| 限制 | 当前值与口径 |
| --- | --- |
| manifest | 20,000 字节 |
| `SKILL.md` | 200,000 字节，UTF-8 frontmatter 与正文 |
| 整个候选包 | 5,000,000 字节；最多 128 个文件或目录条目 |
| 其他资源 | 支持文本与二进制；超过 200,000 字节产生扫描 warning，仍受包总量约束 |
| 候选总数 | 128，包含 pending、publishing、published、rejected |
| 候选存储配额 | 128,000,000 字节；按包实际字节加每项 512,000 字节元数据和 20,000 字节 manifest 预留计费 |
| 单项元数据 / 审阅历史 | 512,000 字节 / 最多 64 次；批准前另预留 8,192 字节容纳发布凭证 |
| 审阅备注 | 最多 4,000 字符；拒绝必填非空白内容，批准可为空 |
| Registry 独立配额 | 最多 512 个发布凭证；每个租户 Skill 32 个历史版本、租户历史总计 256 MiB |

候选没有自动淘汰或用户删除接口；达到配额会拒绝新操作，保留已记录内容。列表一次有界
读取候选元数据，再按项目权限筛选与分页。HTTP 列表跳过无法识别授权范围的损坏记录，
宿主日志记录错误类型；该列表不是损坏数据的完整审计清单。无法核验范围的单项读取
返回 404。内部存储读取默认仍暴露完整性错误，供运维诊断。
HTTP 枚举不恢复候选，也不清理 staging；确认项目可见后才恢复相应候选。提交、内部
恢复枚举和启动恢复可以清理尚未发布为候选的孤立 staging。
上述是容量和单次读取边界，捕获、复核与扫描会重复读取，并非整个操作累计 I/O 上限。

扫描复用 `SkillRegistry.inspect_candidate`：包完整性、frontmatter、资源引用、
凭据样式、危险命令样式，以及大文件和权限 frontmatter 提示。`allowed-tools` 只产生
提示，不授予运行权限。合法但扫描失败的包仍保留 pending，可拒绝；不存在 force 绕过。
批准前服务重新扫描，Registry 在发布锁内再验证一次；新的阻断扫描会写回候选并推进
revision，刷新后可看到失败原因。冻结完整性损坏也会阻止文件读取或批准。

这是一组确定性静态检查，不能证明方法正确、没有所有形式的恶意语义、外传行为、编码
载荷或执行时风险；哈希证明所审字节一致，也不能替代来源认证或人类判断。候选文本及
资源始终作为待审内容展示，不执行其中的 HTML、脚本或指令。

## 审阅事务与恢复

审阅请求绑定候选的 `revision`、`content_hash`，批准还必须携带详情中的完整 `target`：
`current_hash`、`enabled`、`generation`。generation 防止目标经过启用/禁用或版本切换后
又回到相同值的 ABA 冲突。不能只比较包哈希，也不能用旧页面默默覆盖当前选择。

服务持有跨进程候选存储锁，按以下顺序完成批准：

1. 核验当前人类权限、候选 revision/hash、冻结文件与当前扫描；扫描后再次核验权限及
   认证断言有效期。保存 durable `publishing` 意图和人审历史，绑定审阅人、备注、时间、
   候选哈希、review revision 与 expected target 的规范化 SHA-256。
2. Registry 在自己的跨进程锁内 CAS 目标令牌并再次扫描、归档；在最终提交前调用宿主
   权限复验，检查扫描或归档期间发生的成员撤销和断言过期。通过后，将 current pointer、
   `enabled=false`、generation 与 publication receipt 写入同一 canonical state 原子提交。
3. 服务保存 `published` 与精确 receipt。候选 revision 会随状态推进；receipt 的
   `review_revision` 指向人审意图版本，可能小于最终候选 revision。

发布凭证包含 proposal/Skill ID、内容哈希、review revision、reviewer binding、原选择
令牌及发布时间。重复 registry 调用只有全部绑定一致才确认同一凭证，并在候选文件读取
和新的选择写入前返回。普通安装、启用和回滚保留 receipt。

启动的 `recover()` 及内部默认 `get/list` 会收敛 publishing 记录。HTTP 先只读识别
范围，再授权后调用恢复读取；无权查看的请求不会改变候选 revision 或恢复状态：

- 有精确 receipt：只补写候选 published。后来发生的启用、安装或回滚保持原样。
- 没有 receipt：保留该次人审为 `interrupted`，回到 pending 并推进 revision，要求新的
  明确审阅；不会自动重试发布。
- receipt 不匹配或存储无法核验：报告失败，不猜测成功，也不擅自改变当前选择。

候选和 Registry 之间没有跨文件系统数据库事务；durable intent 与 canonical receipt
闭合崩溃窗口。跨进程测试使用本机文件系统锁和真实 spawn/进程终止，不证明分布式锁、
网络文件系统或跨宿主并发语义。HTTP 重发旧 review 请求会得到冲突；底层 receipt 去重
并不意味着 HTTP POST 是无条件幂等接口。

## HTTP 契约

这些是人类控制面的接口，不新增 Agent 工具。列表和读取要求源项目可见，审阅另外要求
写权限及 owner/admin。详情动态加入 `project_name`、当前 `target` 和
`actions.{can_publish,can_reject,reason}`。

| 方法与路径 | 输入与返回 |
| --- | --- |
| `GET /api/skill-proposals` | 可选 `state` 为四种候选状态；省略表示所有状态。`limit` 默认 50，范围 1–100；可选 opaque `cursor` 最长 512 字符。返回 `{items,next_cursor}`。 |
| `GET /api/skill-proposals/{id}` | 完整候选、manifest、来源、description、文件清单、扫描及对应 hash、人审历史、receipt，加动态目标和动作。 |
| `GET /api/skill-proposals/{id}/files` | 必填 query `path` 与 `content_hash`；返回冻结文件预览。 |
| `GET /api/skill-proposals/{id}/download` | 同样的 query；返回完整冻结字节，`application/octet-stream`，attachment、`private, no-store`、`nosniff`。 |
| `POST /api/skill-proposals/{id}/review` | `decision` 为 `approve` 或 `reject`；必填严格整数 `revision` 和包 `content_hash`；批准必填 `expected_target`，拒绝必填非空 `note`。返回新详情。 |

列表按 `(created_at,id)` 倒序，游标来自服务端。每页重新筛选当前权限和状态；分页不是
跨请求冻结快照，新增提案需刷新列表。列表只返回安全摘要；来源 SDK session、相关 Run
摘要、完整扫描、文件及 review/history 在授权详情中。响应不暴露宿主绝对路径或网关
认证 assertion/session 凭据。

文件 query 中的 `content_hash` 是**整个候选包**的哈希；`path` 为包内相对路径，最长
1,000 字符。预览返回 `proposal_id`、包 `content_hash`、`path`、`file_content_hash`、
`size_bytes`、`preview_kind`、`text`、`complete`。文本最多预览 200,000 字节，UTF-8
末尾不完整字符不显示；超过上限时 `complete=false`。二进制 `text=null` 且
`complete=false`，通过 download 检查原件。`SKILL.md` 的容量上限保证有效正文可完整预览。

典型响应：不可见或不存在为 404；可见项目中没有审阅权限为 403；认证过期可能为 401；
revision/hash/target 或扫描、配额冲突为 409；结构校验失败为 422，其他无效请求为 400；
授权后读取的存储或完整性异常为 503。失败详情不返回宿主路径。收到 409 后应重新获取详情，不自动
沿用旧确认。审阅响应丢失时，也先读取 receipt 与状态判断是否已经发布。

## 权限与验证边界

`local_only` 将本地控制面操作者记录为固定 `local-owner`。这依赖本地 API 与宿主的
部署信任，不是登录认证。`trusted_gateway` 使用已认证 principal，并在路由和服务内
检查源项目成员身份、tenant、当前角色及断言有效期；只有 owner/admin 可审阅，普通成员
可按项目写权限提交候选。authenticated 项目不能退回匿名本地审阅。

主 Agent 只有写工作区及显式提交能力；系统提示禁止通过 Bash、HTTP 或修改托管文件
代替人类批准。原生权限、文件围栏及子代理写操作限制继续生效。此实现不等于生产级
进程、凭据、网络或多租户隔离；宿主管理员和本地同用户进程的能力不能靠文件只读位或
提示词消除。正式部署仍需独立的控制面与 worker 隔离方案。

本批验证使用离线 pytest：服务契约、安全文件读取、live assertion 时钟模拟、生产
FastAPI 路由、实际 in-process MCP 分发、真实 spawn 争用与进程中断恢复，以及前端组件
和客户端协议测试。未执行真实 Provider 生成/提交/调用候选的 E2E，也未完成 UI 目视验收；
此前 Green Path 的 Skill 调用报告不证明本功能。

可运行的定向后端检查：

```bash
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_skill_proposals.py \
  backend/tests/test_skill_proposal_process.py \
  backend/tests/test_skill_proposal_api.py \
  backend/tests/test_skill_publication.py \
  backend/tests/test_skill_backup_exercise.py
```

## 备份边界

Registry canonical state 现支持 schema 2，将 generations 和 publication receipts 与
current/enabled 一起持久化；旧 schema 1 可读，在真正变更时迁移。备份脚本的兼容性和
schema 2 receipt 保留已有 pytest 覆盖。

[正式本地独立备份演练](skill-backup-exercise.md) 及其
[20260906T064155970322Z 报告](../evals/reports/skill-backup-20260906T064155970322Z/summary.json)
针对当时 schema 1 的源码指纹与状态。该旧报告不重新背书本批 schema 2 和发布凭证的
正式恢复；当前 v2 备份边界仅为自动化测试，尚未重新生成正式演练报告。

独立 Registry 备份不包含 `data_dir/skill-proposals` 的候选包、来源元数据与人审历史，
也不能只凭 Registry receipt 重建完整候选。要恢复提案流程，需要另行设计并验证候选
存储、相关项目/Run/Artifact 账本和 Registry 的一致备份。当前未提供这项联合恢复演练，
也不证明生产数据灾备或跨版本恢复。
