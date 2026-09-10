# 组织私有 Skill 仓库

对应 PRD §3.7 P2 团队、§4.7 生命周期及 FR-12/22。工作台 **Skills → 私有仓库**
提供部署组织内的已发布包检索、固定版本文件预览与 ZIP 下载，以及管理员导入审核。
所有发布内容仍由原有 `SkillRegistry` 管理，通过 `ophira-tenant` 本地 Plugin 加载。
没有新增模型循环、远程 Git 服务或 Agent 发布工具。

## 操作与权限

1. 所有者或管理员选择 ZIP，点击“上传并准备审核”。上传只形成私有待审副本；
   不进入运行时发现列表，不创建项目、Run 或伪造 Agent 来源。
2. 查看元数据、文件 SHA-256、扫描项和文件正文。文本以普通文本展示，预览先脱敏，
   最多显示 12,000 个字符；二进制和截短内容可下载原始字节检查。
3. 核对文件、扫描和当前仓库版本后，勾选确认并查看具体发布内容，再确认发布。
   阻止发布的扫描必须全部通过，管理员也不能强制忽略。拒绝必须填写原因。
4. 发布原子记录不可变版本和凭证，默认禁用。启用是单独的确认操作；历史版本可以
   核对后恢复，恢复保留当前启用状态。已有 Run 的固定包和活跃会话租约不变。
5. 成员和查看者可读取组织内已发布包、所有历史版本及原始下载。待审、拒绝及
   导入审核历史仅所有者/管理员可见。页面定期及重新获得焦点时复验角色，身份或
   管理权限变化会卸载私有草稿；请求取消后迟到的响应不恢复旧草稿。

`local_only` 使用现有固定 `local-operator`。`trusted_gateway` 使用已有请求绑定的
短期签名断言；接口和提交前重新检查组织、角色及有效期。首次仓库事务将当前
部署身份模式和组织绑定到 `data/skill-imports/scope.json`，之后不能自动换绑。
旧 Installed 页的管理操作也按角色显示；私有包变更核对卡片显示的版本/启用状态，
再携带当前选择代数提交，避免从旧卡片修改后来替换的版本。服务在提交前复验身份。

仓库只服务**一个配置中的部署组织**；`/api/auth/status` 仍报告 `multi_tenant: false`。
多组织数据分区、SSO 网关真实集成、隔离 runner、跨主机同步及生产身份验收仍待交付。

## HTTP 合同

所有 `/api/skills` 响应使用 `Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。
文件下载使用 attachment；公开的版本模型不包含宿主文件路径。

| 接口 | 行为 |
| --- | --- |
| `GET /api/skills/repository` | 返回身份模式、部署组织、当前操作者和管理能力 |
| `POST /api/skills/install?request_id=…&filename=…` | 原始 ZIP 请求体；201 返回待审 `SkillImportView`，替换此前立即安装的 HTTP 语义 |
| `GET /api/skills/imports` | 管理员导入审核列表；按已有凭证核对中断记录 |
| `GET /api/skills/imports/{id}` | 冻结文件清单、扫描、状态、审核历史、发布凭证及当前仓库选择 |
| `GET /api/skills/imports/{id}/files?path=…` | 管理员脱敏文本预览、原字节大小与校验值 |
| `GET /api/skills/imports/{id}/download?path=…` | 管理员原始文件下载 |
| `POST /api/skills/imports/{id}/review` | 明确的人类批准或拒绝 |
| `GET /api/skills/repository/{name}/versions/{hash}` | 已发布固定包的元数据、文件清单和当前选择 |
| 同上加 `/files?path=…` 或 `/download` | 指定文件预览，或整个版本的 ZIP |
| `POST /api/skills/repository/{name}/selection` | 管理员核对后的 enable、disable 或 rollback |

导入响应 `{import, current_target, can_publish}` 中的 `current_target` 为
`{current_hash, enabled, generation}`。批准请求包含 `request_id`、`decision: "approve"`、
导入 `revision`、包 `content_hash`、完整 `expected_target` 和 `note`。拒绝传
`decision: "reject"` 和非空说明，不提交目标选择。客户端不能提交 reviewer、扫描结论、
发布状态或 force 参数。选择操作提交 `operation`、`content_hash`、`expected_target`。
原有 `/api/skills/{namespace}:{name}/versions` 可继续查看历史，命名空间冒号在签名路由中
保持字面形式。

请求键使用 16–100 位字母、数字、下划线或连字符。相同操作者/部署范围内，相同导入键
只接受相同 ZIP 字节；重复提交返回原记录。审核键绑定操作者和完整请求，同键不同内容
为 409。批准时比较当前版本哈希、启用状态和代数，任何并发变化都要求重新加载再核对。
选择变更仅允许紧接提交且结果匹配的重试，不重放已被后续管理操作取代的旧选择。
格式错误为 422，权限不可用为 403，缺失为 404，冲突或无法验证存储为 409；错误不回显
包正文或宿主路径。

## 存储、恢复与上限

- 待审原始文件存于 `data/skill-imports/{id}/package/`，审核记录为 `import.json`。
  目录不属于 Agent 工作区；文件只读，读取拒绝软链接、硬链接、特殊文件及校验值变化。
  ZIP 解包沿用 Registry 的路径、重复条目、压缩大小和内容扫描规则，不执行包内脚本。
- 全部导入事务持有跨进程账本锁。先保存人审 publishing 意图，再调用现有
  `publish_reviewed_candidate`；canonical Registry 原子记录 disabled 选择和发布凭证。
  凭证绑定导入 ID、包哈希、审核修订、操作者绑定值与原选择。
- 进程重启后的管理员列表/详情按凭证恢复：有匹配凭证则补记 published；无凭证则记为
  interrupted 并回到 pending，要求新审核键和新的人类决定。恢复不会重新发布或覆盖
  后来的版本/启用选择；普通网络重试可返回原结果。
- 冻结历史目录的存在不等于发布完成。新导入只有匹配 canonical 凭证后才出现在版本
  列表和下载入口；保存历史包后但提交凭证前的中断不会向成员公开该包，也不能借旧
  rollback 接口选中。导入时已存在的正常历史版本继续可读，不因重复导入被隐藏。
- 上传最多 2,000,000 字节、128 个 ZIP 条目、解压后 5,000,000 字节；人类导入库最多
  100 条、合计文件 128,000,000 字节，单条元数据 512,000 字节、最多 32 次审核。
  原有每技能 32 个不可变版本、历史库 256 MiB 和 512 条共享发布凭证上限继续生效。
  配额满时拒绝新增，不自动删除审核历史或旧 Run 固定的包。
- [应用冷备份](application-backup.md) 验证导入身份、原始文件、审核连续性和 canonical
  凭证。pending/rejected/published 均可复制并恢复到新目录。publishing 或未提交上传
  暂存目录先阻止冷备份；在原安装中由管理员刷新审核列表完成核对/清理，再停机备份。
  快照包含待审原始字节，其访问边界与其余私有应用数据相同。

## 验证

```bash
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest -q \
  backend/tests/test_skill_repository.py backend/tests/test_skill_repository_api.py \
  backend/tests/test_skill_repository_recovery.py backend/tests/test_application_backup.py
PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_skill_repository.py
```

前端在 `frontend/` 执行 `npm run test:run -- src/components/SkillRepositoryPanel.test.tsx`。
专项覆盖人审步骤、原文安全展示、请求重试、Strict Mode、角色变化、迟到响应和中文文案。
后端覆盖签名权限、跨组织拒绝、阻止扫描、固定包、并发比较、真实双进程重复审核、两个
发布中断窗口、提交前断言失效及冷备往返。

独立服务进程的本地 HTTP 演练已验证两个审核导入、两个历史版本、显式启用与恢复、
过期选择拒绝和准确 ZIP 下载，临时数据退出后删除，零模型 Run。
当前 Browser 发现列表为空，未声称浏览器目视通过；真实组织登录、真实模型调用这些
私有包的质量、长期容量及生产隔离验收仍开放。
