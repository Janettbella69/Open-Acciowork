# Skill 本地独立备份恢复演练

`backend/scripts/exercise_skill_backup.py` 验证 FR-12 的本地备份、历史恢复和人工回滚接口。
它从仓库内置 `backend/ophira-capabilities` 复制应用 Plugin，在临时目录生成合成租户
Skill、注册表和运行快照，不构造应用 Settings、FastAPI、Agent 或 Provider。

从仓库根目录执行：

```bash
PYTHONPATH=backend/src .venv/bin/python backend/scripts/exercise_skill_backup.py
```

默认报告输出到新的 `evals/reports/skill-backup-<UTC timestamp>/`，可用
`--report-dir /absolute/new/path` 指定新的报告目录。已有目录会被拒绝。
CLI 的审计守卫拒绝网络连接、域名解析、监听，以及读取 `backend/data` 和 `backend/.env`。
正式演练只使用临时源与目标，保留独立备份、清单、源 inventory 和各案例报告。

## 备份内容与接口

`create_backup(registry, destination, pins)` 接收已经位于隔离目录的 `SkillRegistry`。
它要求 canonical state 已存在，在注册表独占锁内生成完整清单并复制：

```text
backup/
├── manifest.json
└── payload/
    ├── application-plugin/          # 与备份源同版的完整应用 Plugin 文件
    ├── state/
│   ├── registry-state.json      # schema、enabled/current、generations、publication_receipts
    │   └── versions/                # 所有命名空间、所有不可变历史包
    └── pins/<label>/<namespace>/    # 源运行视图的逐文件基线；空 pin 无此目录
```

清单记录每个文件的相对路径、实际字节数、SHA-256、完整逻辑 inventory、命名 pins，
以及演练脚本、Registry、模型定义和协议常量文件的源码指纹。没有 live Skill 的历史包、
禁用版本和非 current 版本都从历史目录独立枚举。完整 enabled overrides 原样保留，
包括目前没有 live Skill 的旧 override。空快照、全部禁用及没有租户历史也可恢复。

`runtime`、`runtime-leases`、`staging`、锁及 legacy `tenant-plugin` 是重建状态，
不进入备份。`install_zip` 仅用于生成演练发布历史，不用于恢复全库。
备份函数不会迁移或修复缺失 canonical state 的源。复制中失败的备份目录应视为未完成；
恢复入口仍会执行所有检查，不把“目录存在”当作成功标志。

`restore_backup(backup, absent_target)` 返回验证报告，不返回指向旧 staging 的 Registry。
目标父目录必须已存在且保持私有、静止；备份也必须在演练期间保持静止。
恢复顺序如下：

1. 验证清单 schema、源码指纹、路径、完整文件/目录集合、大小和实际哈希。
2. 复制到目标父目录中的新 staging。先确认 canonical 存在，再构造新 Registry。
3. 显式对每个历史包调用当前 `SkillRegistry._test_skill`，并扫描所有当前应用 Skill，
   包括尚未进入 versions 的禁用应用 Skill；比较完整 canonical、current、enable 和版本。
4. 通过 Registry 原生接口物化每个旧、新 pin，和备份中的源基线逐字节比较。
5. 重建历史包只读权限，完成验证后原子发布到不存在的目标。

路径穿越、根或祖先符号链接、文件/目录链接、硬链接、特殊文件、清单额外或缺失项、
空历史目录、错误 hash、错误 current pointer 和 canonical 缺失都会拒绝。
特殊文件以非阻塞方式打开后检查类型，FIFO 不会卡住预检。
文件数量上限为 4,096，单文件 5 MiB，每次目录清点的文件内容总量 64 MiB，manifest 2 MiB；
校验、复制、扫描和物化会重复读取，这不是整个恢复操作的累计 I/O 上限。
另限制最多 32 个命名快照、每个快照 128 个条目，以约束空快照和物化工作。
包内容扫描复用现有 Registry 检查，不把 content hash 当作安全扫描替代品。
schema 2 的全部发布凭证与选择 generation 原样复制；每份凭证引用的历史包也必须存在，
即使该包已不是 current、已禁用且不在任何 Run pin 中。恢复前不升级 schema 1；
schema 1 的兼容路径仍由专项测试覆盖，回滚写入时才由 Registry 迁移到 schema 2。

macOS 发布使用 `renamex_np(..., RENAME_EXCL)`；Linux 使用
`renameat2(..., RENAME_NOREPLACE)`。目标在预检后出现也会拒绝，不覆盖它。
本次环境实际验证 macOS 分支；Linux 分支没有在此环境执行。
平台或文件系统不支持该操作时直接失败，没有覆盖式 rename 回退。
平台语义说明见 [Apple 的独占重命名能力](https://developer.apple.com/documentation/foundation/urlresourcevalues/volumesupportsexclusiverenaming)
和 [Linux VFS 的 RENAME_NOREPLACE](https://github.com/torvalds/linux/blob/master/Documentation/filesystems/vfs.rst)。

## 演练与失败证据

源 fixture 包含两组租户 Skill，各有带 references/templates 的 v1/v2 历史，
其中一组启用、一组禁用；包含两个应用禁用项和一个不可发现的旧 enabled override。
当前正式 fixture 通过 Registry 发布方法产生四份 schema 2 凭证，各组 v1/v2 各一份，
发布立即禁用后再恢复 fixture 指定的启停状态。审阅绑定是显式合成值，未经过真人或 HTTP
审批。演练入口要求四份凭证实际存在，空凭证库不能冒充该项验收。
旧、新 pins 保留不同的源运行内容，一个禁用应用 Skill 从未进入历史，必须被单独扫描。
备份完成后临时源根先移走再删除，随后启动全新 Python 进程，只向它传入独立备份、
新目标和报告路径，不传入源目录或任何凭证。

成功 worker 在发布后的最终目标重新构造 Registry 并再次核对 pins 的实际路径与字节，
先核对恢复后的 canonical 文件与备份逐字节一致，再分别对启用/禁用 Skill 调用
reviewed rollback，并比较完整 canonical state，最后复位。
随后用每份凭证的原绑定重试发布，候选路径特意指向不存在的目录。返回值必须与原凭证
完整一致，canonical 文件字节保持不变；旧版本凭证不能撤销后来的版本或启停选择，
也不能触发新的提交。逐项改动内容哈希、审阅 revision、审阅人绑定或原目标 generation，
均必须得到冲突且不改变 canonical 文件。报告分别记录四份凭证的检查结果。
演练直接调用恢复副本的 Registry 方法，验证当前内容扫描与状态转换，
不作为 HTTP 鉴权或真人审批 UI 的验收。
这些发布后检查若失败，报告明确标为失败且 `target_visible=true`；不会声称目标未发布。

另启动八个新进程，分别注入缺失旧历史、文件篡改、无效 current pointer、危险旧历史、
复制中断，以及凭证缺失、审阅人绑定改变、凭证引用的旧历史包缺失。
后三项重新计算运输文件清单，但保留原始逻辑 inventory，分别要求命中逻辑对比或
凭证历史关联检查；不能因提前遇到文件哈希错误就算通过。危险包故障同时重算包地址及
运输清单哈希，必须真正命中当前历史包扫描。
每个案例必须匹配指定失败原因、失败阶段、进程退出码，并证明目标不可见和 staging 已清理；
提前发生的其他错误不能冒充成功的故障演练。成功路径与八个故障报告必须齐全，源码指纹
在演练前后必须一致，主 summary 才可通过。

自动化检查：

```bash
PYTHONPATH=backend/src .venv/bin/pytest -q backend/tests/test_skill_backup_exercise.py
.venv/bin/ruff check backend/scripts/exercise_skill_backup.py backend/tests/test_skill_backup_exercise.py
```

2026-09-06 UTC 的历史 schema 1 正式本地演练通过，完整证据保存在
[skill-backup-20260906T064155970322Z](../evals/reports/skill-backup-20260906T064155970322Z/summary.json)。
独立备份包含 176 个文件、302,997 字节、16 个历史包；成功恢复显式扫描 16 个历史包和
13 个当前应用 Skill。旧、新 pins 分别有 43 和 40 个运行文件，在 staging 与最终目标
两次逐字节核验均通过，启用/禁用回滚保持完整 canonical state。
五个故障均命中指定失败原因与阶段，目标不可见，staging 已清理；47 项定向测试通过。

2026-09-09 UTC 的 schema 2 正式演练通过，完整证据保存在
[skill-backup-20260909T091922465490Z](../evals/reports/skill-backup-20260909T091922465490Z/summary.json)。
独立备份包含 176 个文件、305,589 字节、16 个历史包和四份发布凭证；恢复扫描
16 个历史包和 13 个当前应用 Skill，旧、新 pins 的 43/40 个文件两次逐字节一致。
四次匹配凭证的重试保持状态不变，16 次单字段不匹配重试均被拒绝；两份历史版本凭证
不会覆盖已更新的选择。一个成功场景和八个故障场景在九个不同新进程中全部达到预期。

另使用独立的标准库哈希/JSON 检查，复算备份的 176 份文件、16 个包地址，比较
25 份 pin 包副本，并核对九份进程报告及四份凭证，未调用演练脚本或 Registry 验证函数；
结果见 [independent-check.json](../evals/reports/skill-backup-20260909T091922465490Z/independent-check.json)。
该复核面对的是保留的备份和进程报告；临时恢复目标已被演练清理，不把报告核对描述为
再次运行恢复。59 项备份专项测试、后端全量 1478 passed / 8 live-only skipped、
Ruff 和 `git diff --check` 通过；本批未改前端。

## 证明范围

manifest 是本地完整性清单，不是签名或来源认证。能重写备份和清单的人可以构造新的
一致内容，因此备份信任、签名、加密、异地保管和访问控制仍需要独立基础设施方案。
脚本要求相同的相关源码指纹；跨版本恢复需要另行设计兼容和迁移流程。
本演练证明同一主机的本地独立副本可在新进程恢复，不证明跨宿主灾备、加密 offsite、
生产数据恢复、跨租户隔离或真实 Provider 的回滚后重跑。
本次范围只包含 Skill Registry、包历史和指定 pins；提案候选库、项目账本与 Registry
的一致应用备份仍未实现。凭证对比不能证明审阅人身份真实；能够同时改写 canonical
和逻辑 inventory 的人不受未签名清单约束。

设计依据为 PRD v2 §4.7/§6.4、仓库本地官方 SDK 快照
[Skills](claude-agent-sdk/skills.md) 和 [Plugins](claude-agent-sdk/plugins.md)。
恢复继续使用文件系统 Plugin 和原生 SkillRegistry 物化接口，没有建立模型调用循环。
