# Schedule 专属经验（PRD §4.16）

每个 Schedule 可积累少量、可追溯的抓取/测量经验，供下次同任务运行参考。经验是 Agent 形成的**低信任观察数据**，不是用户策略，也不是已批准的全局 Memory。写一条经验无需逐条人审；人工可以查看版本和来源、停用错误经验。

## 功能与边界

- 唯一新增自动化域工具：`mcp__ophira_automation__record_schedule_experience`。与发通知独立，因此安静的 heartbeat 也能积累经验，不会为了写经验制造通知。
- 工具参数仅有稳定 `key`、`category`（`selector_failure` / `threshold_observation` / `retrieval_lesson`）、`observation`、`lesson`、可选 `source_event_ids`。没有 Schedule、项目、Run、身份、权限、预算或触发条件的可写参数。
- 宿主从当前绑定的 scheduled Run 恢复 scope；普通对话、standing-intent 检查、跨 Schedule/项目/身份、缺少 SDK session 或非 running Run 都不能写。认证项目须以当前成员账本重新核验权限；撤销和角色降级 fail closed。
- SDK 只读子代理的 PreToolUse/can_use_tool 边界拒绝此工具。存储层也要求宿主记录的同参数主 Agent `tool.started` 事件，拒绝分支 actor。工具不在只读白名单；在既有 envelope-approved 自动化列表，避免为观察笔记错误增加人审。
- 经验模块从不调用 Schedule 更新、全局 Memory 或业务触发/预算更新接口。所谓阈值校准仅记录“测量噪声/建议重新验证”的观察或假设，不自动改变用户已指定的告警阈值。
- 外部内容仍可以作为观察的素材，但不能冒充 system/user 指令、已批准策略或权限。注入使用项目既有 untrusted sanitizer/envelope；结果正文不回填可信系统提示。工具成功结果仅返回版本、指纹、来源等元数据。

## 版本、容量与恢复

`data/task-experiences/{schedule_id}.json` 存放独立经验账本（schema v1）；不进入项目事实、agent.md 或 Memory 蒸馏输入。

| 上限 | 行为 |
| --- | --- |
| 每 Schedule 24 个条目 | 满额拒绝新 key，提示人工停用过时条目；新增时只释放最早停用条目，并累计 `evicted_retired_entries` |
| 每条最近 4 个版本 | 单调 version，记录 `pruned_versions`；不是永久完整版本库 |
| 每 Run 8 个 key 写入 | 同 Run + key + 相同内容幂等；同 key 不同内容拒绝，留待下次运行 |
| observation / lesson 各 800 字符 | 空白、未知字段、超长内容由 Pydantic / MCP schema 拒绝 |
| 注入上下文 12,000 字符 | 优先最新、完整条目；携带省略计数，并经过既有安全文本边界 |

每次记录含 source Run、trace、SDK session、写入事件 ID、来源事件 ID / SHA-256、配置指纹、请求内容指纹、origin、trust 和时间。已知来源 ID 会严格验证为同 Run、同 SDK session 的更早 `tool.completed` / `tool.failed`；省略时宿主取同 session 最近最多三个非经验工具结果。写入调用也校验 Run、项目、trace 和 SDK session。**这些锚点证明记录的调用/结果来源，不证明模型总结正确，也不保证自动选择的结果正好支持每句话。** 内容指纹对应脱敏后的工具请求（含调用者给出的来源列表）；实际解析后的来源和独立事件 hash 另存。

SDK 模型通常看不到宿主 Event 序号，故来源列表不能设计成不可操作的必填字段。自动绑定不增加只为查询 Event ID 的新工具，也不会将外部报告冒充策略层。

调度器从生成本次 prompt 的同一份 Schedule 快照计算配置指纹，RunManager 在发布 `run.created` 和启动 SDK 之前将其写入 `RunRecord.task_experience_config_hash`。首次 query 与后续记录均比较此 dispatch 指纹和当前配置；即使配置在 Run 创建前后、首次 query 之前改变，也不会给旧 prompt 配上新配置的经验。缺少宿主 dispatch 指纹的旧 Run 不注入、不写入经验，不能通过读取当前 Schedule 猜补授权快照。

`runs/{run_id}/task-experience-context.json` 保存与 dispatch 一致的配置指纹、本次注入文本/指纹、时间和最多八个幂等收据。新 Run 仅选同 Schedule/项目且配置一致的未停用最新版本；同 Run 反复准备不会读到本 Run 刚写入的经验。运行收据随普通 Run 历史保留，不是无界的全局幂等表。经验聚合快照的版本裁剪不破坏已有 Run 的幂等收据。

FileLedger 的跨进程 flock 与原子写/fsync 被复用，经验事务与 Schedule 修改使用同一 Schedule 锁，并按 Schedule → Run 顺序锁定、重新读取运行状态。因此等待锁期间已取消的 Run 不能沿用旧 running 状态写入。一个有界的 `{schedule_id}.transaction.json` 预写记录先于经验快照和 Run 收据持久化；任何后续经验读写都先恢复未完成事务，再推进版本/裁剪。因而进程在两个文件之间崩溃也不会造成丢失收据、重复增版或后续裁剪破坏幂等性。恢复完成后删除预写文件并 fsync 目录；它不是永久追加的大日志。

配置指纹包括 prompt、触发定义、timezone、kind/mode、provider、agent、权限上限和授权绑定等；不包括 last_run、last_fire、next_fire、enabled 等调度记账。配置不同时管理页标记 stale，下次 Run 不注入旧经验，旧 Run 的后续写入拒绝。暂停/恢复不更改策略定义，不会仅因调度记账误判 stale。

停用语义是有界保留期内的条目纠错，不是永久 key 封禁。停用条目仍保留时，Agent 不能重新激活它；满额释放该条目后，后续运行可以形成相同 key 的**新条目**，但其 `experience_*` ID 必须重新生成，旧条目的 ID/版本身份不复用。版本号只在同一个条目中单调递增。旧 Run 的幂等收据仍返回原 source Run/事件对应的记录，不会改写或冒充后来同 key 的新条目；没有永久 tombstone/黑名单承诺。

## 接入合同与管理入口

```python
experiences = TaskExperienceStore(ledger, access=project_access)
# SDK 发起 query 前：只将返回值加入任务/查询 context，不放入安全 policy prefix。
task_context = experiences.context_for_run(run)
# 普通 Run / standing-intent 返回空字符串；scheduled Run 首次冻结上下文。
build_automation_mcp_server(..., task_experiences=experiences)
register_task_experience_routes(
    app, experiences=experiences, ledger=ledger,
    require_project_authorization=require_project_authorization,
    project_is_visible=project_is_visible,
)
```

`GET /api/schedules/{id}/experiences`：查看受项目读取授权保护的版本、来源、stale/retired、容量信息。`POST /api/schedules/{id}/experiences/{entry_id}/retire` 接收 `expected_version` 与 `reason`，要求项目写权限，版本冲突返回 409；没有 API 可把人/外部提交直接写成 Agent 经验，也没有逐条“批准”接口。保留中的停用条目不会由 Agent 自动重新激活。

Scheduled Tasks 页的“任务专属经验 / Task experience”面板按需加载，显示低信任说明、配置失效、版本/来源和人工停用原因；未知权限/冲突错误可见，可刷新重试。正文以 React 文本渲染，不执行 HTML。停用用于后续新运行选择；已经交给正在运行的模型的历史上下文无法被追溯抹除。

## 验证

隔离测试使用 `tmp_path`，无真实项目内容、secrets、模型、付费服务或外部站点请求。覆盖配置失效/身份撤销、调度快照在创建与首次 query 前后的竞态（普通任务及 heartbeat）、取消期间禁止写入、同 session 溯源、主 Agent trace 与 SDK 分支门禁、真实 SDK in-process MCP schema/error 结果、四个独立 Python 进程并发/幂等、版本和容量、崩溃窗口恢复、API 授权及前端低信任/停用流程。

```sh
PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests/test_task_experience.py backend/tests/test_automation_tools.py -q
.venv/bin/ruff check backend/src/ophira_backend/task_experience.py backend/src/ophira_backend/task_experience_routes.py backend/tests/test_task_experience.py
cd frontend
npm test -- --run src/components/TaskExperiencePanel.test.tsx src/components/ScheduledTasksPage.test.tsx
npm run lint
npm run build
```

读取了仓库 SDK custom-tools / permissions / hooks / modifying-system-prompts 文档，并核对固定安装版本的 `create_sdk_mcp_server` 实现。未声称完成真实 Provider 自主选择该工具、长期计划运行质量或浏览器像素级验收；这些与本地持久化/门禁契约测试是不同验证层。
