# 应用数据冷备份与恢复

该入口把整个应用 `data_dir` 和配套应用 Plugin 放入同一个本地快照，包含项目工作区、
Run/事件、提案冻结包和审阅记录、Skill 历史/发布凭证，以及该目录中的授权、记忆、
配置、凭据和会话文件。恢复仅发布到不存在的新目录，不启动 FastAPI、SDK、计划、
通知或商业动作。它不读取 `backend/.env`，不调用外部服务。

## 本批要求清单

| 要求 | 状态 |
| --- | --- |
| 整体捕获项目账本、提案候选库和 Skill Registry | delivered：完整目录快照；源目录删除后的独立进程恢复已归档 |
| 应用构造和整个运行生命周期与冷备份互斥 | delivered：共享/独占进程租约，真实 spawn 与应用生命周期测试，保留路径别名兼容和动态权限配置 |
| 恢复前检查来源、固定包、扫描和发布凭证关联 | delivered：逐项校验；运输清单重新计算的语义篡改也会拒绝 |
| 中断提案按已有凭证恢复，不重新执行发布 | delivered：两个中断窗口恢复；Registry 字节不变；异常恢复写入拒绝 |
| 备份/恢复不覆盖已有目录，私有权限、容量与路径限制 | delivered：独占原子发布、并发目标保护、私有权限与合计配额；路径/软硬链接/FIFO 测试通过 |
| 本地删除登记恢复对账 | delivered：项目、独立会话、记忆墓碑、三个应用 vault 的凭据删除哈希链及店铺浏览器登录删除边界；须提供最新停机目录或显式承认无法对账 |
| 在线热备份、分布式租约、跨版本迁移、加密异地灾备 | deferred：不属于本次停机本地快照的实现范围，需要独立协议和验收 |
| SDK 跨路径/跨宿主会话续接 | deferred：SDK 会话依赖原工作目录，不能仅靠文件复制证明可续接 |

## 操作

停止所有使用源数据目录的 API、worker、浏览器及独立维护程序，并确认 Run 均已终态。
等待审批、排队、执行或暂停的 Run 会使备份失败；须在应用内完成或取消，并完成正常关闭。
项目或会话删除仍处于 deleting/blocked，或其登记目录含未提交的残留文件时，
备份也会失败；先在原安装中核对并完成删除。记忆墓碑若仍对应正文、历史或 Agent
记忆镜像或未清理的私有暂存文件，备份同样拒绝，须先完成原操作的中断恢复。
Provider、连接器或项目 MCP 的本地密钥仓若留有未完成的删除意图或私有暂存文件，
备份也会拒绝；先用原安装的当前程序完成启动恢复并正常停机。
店铺浏览器登录删除后若仍有旧代次 profile、旧会话或孤儿登录目录，备份同样拒绝；
先在原安装中重试清理并关闭窗口。
新 API 从构造开始以及整个 ASGI lifespan（含启动恢复与关闭等待）持有共享租约；
备份使用非阻塞独占租约，忙时立即失败。首次升级时必须先停止旧版本进程，旧代码不认识
新锁；自行绕开应用入口或关闭 lifespan 的服务不在该租约协议保证范围内。
Plugin 也须在备份期间保持静止，不同时部署/编辑。

从仓库根目录执行，替换示例路径；输出目录必须不存在，父目录必须已经存在且由操作者控制：

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.application_backup create \
  --data-dir /absolute/app-data \
  --plugin-dir /absolute/ophira-capabilities \
  --output /absolute/private-backups/snapshot-001

PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.application_backup restore \
  --backup /absolute/private-backups/snapshot-001 \
  --current-data-dir /absolute/app-data \
  --output /absolute/private-restores/recovered-001
```

恢复时必须选择一个删除历史处理方式。若仍能取得**最新且已停机**的应用数据目录，
使用 `--current-data-dir`。CLI 对该目录持有独占维护租约，验证项目与会话的删除登记
均已完成，再将其登记文件、目录和哈希、记忆删除墓碑、三个本地密钥仓的删除哈希链
及店铺浏览器登录删除边界
与候选恢复目录逐项比较；
不一致即拒绝发布。候选备份中被墓碑覆盖的记忆正文、历史和 Agent 镜像也会被拒绝。
当前目录不得取自该备份内部。若原目录已经丢失，只能改用
`--acknowledge-unreconciled-history` 明确承认无法对账后恢复。结果中的
`deletion_reconciliation` 会记录本次对账范围与数量；无法对账时为 `null`。
此门禁覆盖应用内项目、独立会话、记忆、本地密钥仓和店铺浏览器登录数据的删除登记。它不能证明主机
环境变量、外部服务令牌、下载副本或历史备份已擦除。旧快照不会被自动修改或销毁。

备份输出：`manifest.json` 和 `payload/{data,application-plugin}/`。
恢复输出：`{data,application-plugin}/`，可在停机检查后分别配置为 `OPHIRA_DATA_DIR`
和 `OPHIRA_CAPABILITY_PLUGIN_DIR`。CLI 只输出成功/失败、稳定原因、数量和恢复状态，
不输出项目正文、提案内容、凭据或任意异常文本。`target_visible=true` 时目标已存在，
不能因为命令失败就自行删除它。

首次恢复检查应在隔离环境进行。目录包含原有计划、通知和外部服务配置；启动真实应用会
重新加载这些配置，需要操作者决定何时恢复服务。环境变量、`.env`、操作系统钥匙串、
外部服务状态和数据目录之外的 SDK/浏览器文件不在快照里，须由部署方单独保存与配置。
对账需由操作者提供可信的最新目录；恢复程序无法从一份孤立旧快照推断其后发生的删除。
必须使用包含此门禁的当前恢复程序；旧版 CLI 不会执行新对账。对账未通过的目标不会发布，
但备份本身依然存在，须按备份保留政策另行清理。

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
- 项目与会话删除登记各自最多扫描 20,000 个文件、40,000 个条目和 512 MiB，
  且只接受已完成删除的标准登记文件；超限或未完成操作须在原安装中处理。
- 记忆目录按全量备份限制扫描；最多核对 100,000 条墓碑。正常记忆更新不会造成墓碑
  对账失败，但恢复旧快照仍可能丢失该快照之后新增的未删除记忆。
- 凭据登记为每个 vault 的定长计数与哈希链，记录从本版程序开始执行的非空删除；
  首次新增不会造成登记差异，替换旧值及令牌轮换会记录旧值删除。删除前先落盘意图，
  写入新 vault 和登记后才完成；
  中断由下次 vault 访问恢复。旧版程序此前执行的删除无法追溯补记。
- 店铺登录删除边界保存在店铺身份记录中：普通身份编辑与后续新代次登录不会造成
  对账差异；一次删除意图会推进单调计数和最后清理代次，冷备拒绝该代次及更早的
  profile 或无法证明属于新代次的 session。中断清理须在原安装重试。
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

[插件包导入](plugin-imports.md) 报告也纳入冷备核对：部署范围、报告身份、子 Skill 导入请求、
上传者、扫描结论和固定内容哈希必须对应。MCP-only 插件同样保留部署范围；未完成的插件
扫描暂存目录会阻止备份。恢复后报告仍指向原来的逐件 Skill 审核记录，MCP 地址不会自动启用。

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests/test_application_backup.py
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests
.venv/bin/ruff check backend/src backend/tests backend/scripts/exercise_skill_backup.py backend/scripts/exercise_application_backup.py
```

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
