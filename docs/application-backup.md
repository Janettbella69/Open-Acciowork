# 应用数据冷备份与恢复

该入口把整个应用 `data_dir` 和配套应用 Plugin 放入同一个本地快照，包含项目工作区、
Run/事件、提案冻结包和审阅记录、Skill 历史/发布凭证，以及该目录中的授权、记忆、
配置、凭据和会话文件。恢复仅发布到不存在的新目录，不启动业务应用、SDK、计划、
通知或商业动作。它不读取 `backend/.env`，不调用外部服务。

另提供独立的本机维护界面，入口说明在 **Settings → Backup and recovery**。
该界面复用下面的冷备份和恢复校验，提供副本核对、确认发布、丢弃未提交副本和中断重试；
它不在正在运行的应用中执行冷备份，也不会自动切换现有部署。

## 本批要求清单

| 要求 | 状态 |
| --- | --- |
| 整体捕获项目账本、提案候选库和 Skill Registry | delivered：完整目录快照；源目录删除后的独立进程恢复已归档 |
| 应用构造和整个运行生命周期与冷备份互斥 | delivered：共享/独占进程租约，真实 spawn 与应用生命周期测试，保留路径别名兼容和动态权限配置 |
| 恢复前检查来源、固定包、扫描和发布凭证关联 | delivered：逐项校验；运输清单重新计算的语义篡改也会拒绝 |
| 中断提案按已有凭证恢复，不重新执行发布 | delivered：两个中断窗口恢复；Registry 字节不变；异常恢复写入拒绝 |
| 备份/恢复不覆盖已有目录，私有权限、容量与路径限制 | delivered：独占原子发布、并发目标保护、私有权限与合计配额；路径/软硬链接/FIFO 测试通过 |
| 完整备份和新目录恢复的中英文 UI | delivered：独立 loopback 维护服务、私有会话、范围/目标核对、确认发布、持久重试和设置页指引；组件及真实 HTTP 合成演练通过，浏览器目视未验收 |
| 在线热备份、分布式租约、跨版本迁移、加密异地灾备 | deferred：不属于本次停机本地快照的实现范围，需要独立协议和验收 |
| SDK 跨路径/跨宿主会话续接 | deferred：SDK 会话依赖原工作目录，不能仅靠文件复制证明可续接 |

## 操作

停止所有使用源数据目录的 API、worker、浏览器及独立维护程序，并确认 Run 均已终态。
等待审批、排队、执行或暂停的 Run 会使备份失败；须在应用内完成或取消，并完成正常关闭。
新 API 从构造开始以及整个 ASGI lifespan（含启动恢复与关闭等待）持有共享租约；
备份使用非阻塞独占租约，忙时立即失败。首次升级时必须先停止旧版本进程，旧代码不认识
新锁；自行绕开应用入口或关闭 lifespan 的服务不在该租约协议保证范围内。
Plugin 也须在备份期间保持静止，不同时部署/编辑。

### 本机维护界面

从 `frontend/` 运行一次 `npm run build`，构建包含 `maintenance.html` 的完整前端。
使用仓库已安装依赖的 Python 环境，在后端计算机上启动：

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.maintenance_console \
  --data-dir /absolute/app-data \
  --plugin-dir /absolute/ophira-capabilities \
  --workspace /absolute/private-maintenance
```

`--workspace` 是与 data/Plugin 无包含关系的新私有目录，父目录须已存在。已存在的维护
目录必须属于当前 OS 用户且没有 group/other 权限；服务不会修改任意已有目录的权限。
首次使用时绑定 data/Plugin 路径，后续启动不能把相同维护目录切换到另一安装。
源目录已丢失时仍能进入界面，从现存备份恢复。

维护程序仅监听 `127.0.0.1`，默认由 OS 分配空闲端口，可通过 `--port` 固定。
控制台打印带有随机会话密钥的专用链接；仅供本机操作者使用，请勿分享。密钥放在 URL
fragment，页面读取后清除，后续仅随授权头发送，不存入浏览器存储；8小时或进程退出后
失效。重新打开不带密钥的页面时，可输入原会话密钥；过期后须重新启动维护程序获取新链接。
服务校验 loopback 客户端、固定 Host、同源请求和显式确认，禁止远程绑定、代理头信任、
开放 CORS 和自动重定向；密钥不写入 HTTP 路径/访问日志。服务不构造主应用或启动后台任务。

1. **Prepare backup review** 捕获源数据和 Plugin，复用停机独占租约及全部结构校验。
   捕获副本保存在维护目录的私有暂存区；应用仍在运行或有未完成 Run 时拒绝捕获。
2. **Prepare restore review** 从 `workspace/backups/` 的目录列表选择备份。已有 CLI
   冷备份也可由操作者放进该目录（整个 `manifest.json` + `payload/` 目录，不是压缩包）
   后刷新；HTTP 请求只接受列表 ID，不能提交任意宿主路径或上传文件。
   服务在副本上执行同版本、哈希/逻辑关联验证和中断提案恢复，原备份不变。
3. 界面展示核对时间、项目/Run/文件数量、恢复后的提案状态及将创建的绝对目录；核对时间
   不是原始备份的数据时间。明确提示凭据/登录态/自动化在副本内、目录未加密，以及原生
   会话的原工作目录限制。阅读后勾选确认，发布已经验证的副本。核对24小时有效。
4. 确认前复验当前会话有效期、后端代码指纹、暂存目录身份和全部字节。`publishing`
   记录持久化后操作已提交，关闭网页或断网不会取消；重复请求继续同一次独占原子发布。
   rename 后进程退出时，用目标目录 inode 和全量指纹确认已经发布，再补记完成状态。
   目标冲突不覆盖，已提交操作不能丢弃。未提交副本可明确丢弃，原数据/备份不受影响。
5. `workspace/backups/backup-<request_id>/` 是新备份；
   `workspace/restores/restore-<request_id>/{data,application-plugin}/` 是恢复结果。
   页面给出两个路径供本机操作者配置。**启用恢复副本是另一个部署动作**：先停止旧应用，
   核对恢复的自动化、外部连接和凭据，再决定重启时间。复制文件不代表跨路径 SDK 续接成功。

同一维护目录通过跨进程锁串行操作，最多一个未完成核对、200条持久操作记录。
记录只保留请求/安装身份摘要、时间、指纹和数量，不含正文或密钥；实际私有暂存副本当然
包含完整数据。一次副本沿用下文2 GiB内容限制，备份运输包另外容纳有界 manifest。
容量到限后换用新的私有维护目录；保留已完成记录用于幂等查询，不静默删除历史备份。
中断的准备可从同一请求重新开始；已提交的发布只能恢复原副本。目录被替换、文件变更、
代码版本不匹配等情况均拒绝继续，须由操作者检查，不清理未知的已有目录。

维护 API 只存在于上述独立服务：`GET /api/maintenance`、
`POST /api/maintenance/reviews`、`POST /api/maintenance/reviews/{request_id}/confirm`、
`DELETE /api/maintenance/reviews/{request_id}`。所有 API 需要会话密钥；变更还要求同源
Origin。JSON正文限制2048字节，确认必须是严格布尔值，响应禁止缓存。

### 命令行

从仓库根目录执行，替换示例路径；输出目录必须不存在，父目录必须已经存在且由操作者控制：

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.application_backup create \
  --data-dir /absolute/app-data \
  --plugin-dir /absolute/ophira-capabilities \
  --output /absolute/private-backups/snapshot-001

PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.application_backup restore \
  --backup /absolute/private-backups/snapshot-001 \
  --output /absolute/private-restores/recovered-001
```

备份输出：`manifest.json` 和 `payload/{data,application-plugin}/`。
恢复输出：`{data,application-plugin}/`，可在停机检查后分别配置为 `OPHIRA_DATA_DIR`
和 `OPHIRA_CAPABILITY_PLUGIN_DIR`。CLI 只输出成功/失败、稳定原因、数量和恢复状态，
不输出项目正文、提案内容、凭据或任意异常文本。`target_visible=true` 时目标已存在，
不能因为命令失败就自行删除它。

首次恢复检查应在隔离环境进行。目录包含原有计划、通知和外部服务配置；启动真实应用会
重新加载这些配置，需要操作者决定何时恢复服务。环境变量、`.env`、操作系统钥匙串、
外部服务状态和数据目录之外的 SDK/浏览器文件不在快照里，须由部署方单独保存与配置。

## 一致性与恢复顺序

1. 拿到源数据目录独占租约，清点全量文件和空目录，记录相对路径、owner 模式、大小和
   SHA-256；只排除本次使用的 `.application-maintenance.lock` 文件，锁本身不属于业务数据。
2. 在目标父目录的私有 staging 中复制数据与 Plugin；在副本上检查语义，原数据不做
   提案恢复或 Registry 迁移。复制后再次对比源清单，数据变化则丢弃未发布备份。
3. 深度校验覆盖所有项目/Run 记录（不使用会跳过坏记录的 UI 列表）、Run 项目与身份范围、
   Run 终态、固定 Skill 快照、所有历史/当前包扫描，以及提案的冻结字节、来源 Run/session、
   Artifact checksum、历史 Run 引用和人审/发布凭证关联。危险但尚待审的候选保留待审，
   不被扫描通过要求错误地当作已安装包，也不会因恢复而启用。
4. manifest 记录完整运输清单、逻辑 inventory 和当前后端全部 Python 模块指纹。恢复要求
   相同指纹，先对比实际文件再复制到新的 staging，禁止清单指向任意外部路径。
5. 恢复副本按现有 `SkillProposalService.recover()` 处理 publishing：有匹配凭证则补记
   published；无凭证则增加 revision、记为 interrupted 并回到 pending，要求重新人审。
   Registry canonical 字节必须不变；原 pending/rejected/published 不重写。除 publishing
   的 record、首次生成的空提案锁、被清理的未提交 staging 外，额外数据变更会使恢复失败。
6. 校验通过后 fsync 文件与目录，并用系统的独占 rename 发布整个根目录；目标即使在
   检查后由另一进程创建也不会覆盖。只清理当前调用持有的 staging，不删除已有部署。

其他存储的原始字节也完整保留，但本批未为每个渠道、自动化、记忆或外部副作用记录实现
专门的跨对象业务校验；不得把全量文件快照描述为所有子系统的生产恢复认证。

## 限制与权限

- 默认最多 50,000 个文件、100,000 个条目、单文件 64 MiB、文件内容总计 2 GiB、
  manifest 32 MiB、32 层目录。校验会多次读取，限制不是累计 I/O 预算。
- 所有路径组件用目录描述符解析，拒绝软/硬链接、FIFO、设备等特殊文件；备份无法包含
  含这些条目的源目录。支持空目录与 owner 可执行位，清除 group/other 权限，最终根目录 0700。
- 快照包含凭据文件，采用本地私有权限保存，**没有新增备份加密或签名**。能同时改写文件
  和 manifest 的操作者可制作新的自洽快照；哈希不是身份认证，也不能替代可信存储。
- 锁和原子目录发布限本机支持的文件系统；macOS/Linux 分支沿用独立 Skill 备份的
  no-replace 实现。断电、磁盘损坏、远程文件系统、攻击者拥有相同 OS 权限等未由本批认证。
- 本地 SDK [会话存储说明](claude-agent-sdk/session-storage.md) 明确 session key 与
  工作目录关联。恢复到新路径保留文件，不改写 SDK 内部路径或自动续跑；要恢复原会话，
  还需部署方恢复匹配目录和配置并独立验收。

## 验证命令

项目模板也纳入结构校验：不可变版本、固定参考字节、创建记录和新项目来源必须
一致。未完成模板保存、未完成创建或残留创建暂存目录会阻止冷备份；先在原安装中
核对并重试完成，再制作备份。已完成模板和项目可恢复到新目录，见
[项目模板及创建恢复](project-templates.md)。

[私有 Skill 导入](private-skill-repository.md) 也纳入结构校验：部署范围、冻结原文件、
包元数据、审核修订及 Registry 发布凭证必须一致；pending、rejected 和 published
记录可以恢复。未核对的 publishing 或残留上传暂存目录会阻止冷备份，先在原安装中
由管理员刷新导入审核列表，按已有凭证完成核对并清理未提交暂存，再正常停机备份。
该核对只补记结果，没有凭证时要求重新人审，不自动重新发布。

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests/test_application_backup.py
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests
.venv/bin/ruff check backend/src backend/tests backend/scripts/exercise_skill_backup.py backend/scripts/exercise_application_backup.py
```

维护服务的合成验收：

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests/test_maintenance_console.py -q
PYTHONPATH=backend/src .venv/bin/python backend/scripts/exercise_maintenance_console.py
```

后一个命令先要求已构建前端，启动真实 loopback 子进程，检查编译 HTML/CSS/JS/品牌资源
及授权/确认，再通过 HTTP 完成备份、重复确认、源删除后的恢复与损坏备份拒绝。
父子进程只允许 loopback 网络，阻断正式数据、`.env` 和宿主 Claude 配置读取；
不使用真实项目、不调用模型。它会自动关闭临时服务和清理合成目录，不是浏览器目视验收。
最近报告：[maintenance-console-20260922T211420980561Z](../evals/reports/maintenance-console-20260922T211420980561Z/summary.json)。

## 2026-09-09 验收记录

正式合成演练：

```bash
PYTHONPATH=backend/src .venv/bin/python backend/scripts/exercise_application_backup.py
```

归档：[application-backup-20260909T095925318909Z](../evals/reports/application-backup-20260909T095925318909Z/summary.json)。
一份独立快照包含 146 个文件、165 个目录、192,446 字节、一个项目/Run、五份提案和两份
发布凭证，包含仓库的 13 个内置 Skill。源目录删除后，五个不同进程分别执行成功恢复、
提案缺失、凭证缺失、Run/session 来源错误与文件篡改，均命中预期结果。恢复结果为
两个 pending、两个 published、一个 rejected，canonical 字节保持不变，备份本身未改动。
演练在父/子进程安装网络和正式数据访问守卫；没有启动真实模型或商业动作。

独立标准库复核重新计算全部文件哈希和权限，核对冻结包、项目/Run/Artifact 来源、凭证
和五份进程报告，结果见 [independent-check.json](../evals/reports/application-backup-20260909T095925318909Z/independent-check.json)。
它核对保留的快照与报告，不将已清理的临时恢复目标描述为再次恢复。

归档同时保留 [backup.tar.gz](../evals/reports/application-backup-20260909T095925318909Z/backup.tar.gz)，
以保存 Git checkout 不保留的只读文件位和空目录。复现恢复时，将这个可信合成归档解包到
新的私有目录并保留 owner 权限，再将解包后的 `backup/` 传给 CLI；不要直接使用 checkout
产生的展开副本。另一个全新 CLI 进程已经从解包副本完成恢复，结果见
[archive-roundtrip.json](../evals/reports/application-backup-20260909T095925318909Z/archive-roundtrip.json)，
其中记录压缩包 SHA-256、102,222 字节大小及相同的提案恢复结果。

35 项本批专项测试纳入后端全量：**1513 passed / 8 live-only skipped**（70.89s）；
Ruff 和 `git diff --check` 通过。测试包含真实进程抢锁、应用构造/启动/关闭租约、路径别名、
权限动态撤销、两个提案中断窗口、额外恢复写入、源变化、合计容量、特殊文件和失败输出脱敏。
未改前端，未重跑其测试或宣称 UI 备份交付。
