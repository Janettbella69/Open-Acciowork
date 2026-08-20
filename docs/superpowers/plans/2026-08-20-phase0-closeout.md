# OPHIRA Phase 0 收口（Green Path + 评测 + 前端两页 + CLAUDE.md）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成 PRD v2.1 Phase 0 出口标准：Green Path E2E 真端点 3 连过、providers/skills/preflight 单测与 Provider 合约测试报告、前端定时任务页与设置页 Provider UI 接真后端、§3.11 评测体系脚手架落地，并按用户目标创建根 CLAUDE.md。

**Architecture:** 不改变既有架构（Claude Agent SDK 单主 Agent + FastAPI 控制面 + 文件账本 + React 前端）；本计划只做"打通、验证、收口"。Green Path 复用真实 `ClaudeAgentRuntime` 在 in-process FastAPI 应用上驱动，产出可审计 run bundle 到 `evals/reports/`；评测 L2 直接复用该 harness。

**Tech Stack:** Python 3.11 / FastAPI / claude-agent-sdk 0.2.82（钉住，不升级）/ pytest；React 19 / Vite 7 / vitest。

**Spec:** `docs/OPHIRA-PRD-and-Technical-Architecture-v2.md`（v2.1）§3.7 P0、§3.11、§5 Phase 0、§6.1–6.6；工程规则 `AGENTS.md`。

## Global Constraints

- 红线：核心 Agent 只走 `ClaudeSDKClient`/`query()`；禁 OpenAI/Anthropic Client SDK、LangGraph/LangChain/Dify、手写工具循环（§1.1）。
- `tools`/`system_prompt` 用 `claude_code` preset；`setting_sources=[]`；Skills 只经 plugins 装载；`strict_mcp_config=True`。
- `sandbox.enabled=True` 且 `allowUnsandboxedCommands=False` 不得关闭；worker env 必须先清空 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN` 三个凭据变量（§6.1）。
- 普通模式 `disallowed_tools=["Agent"]`、`agents=None`（放宽提案待拍板，维持现状，测试断言不动）。
- SDK 版本钉 0.2.82；本计划不升级（升级须走合约测试，PRD §7）。
- 评测门槛口径数值留空（PRD §3.11：不预填演示数字）；judge gold 由项目负责人本人标注，本计划只建模板。
- 密钥只存 `backend/data/secrets/`（0600），不进代码、事件、报告；报告写入前过 `redact()`。
- 前端后端各自目录内工作；跨界只经 API/事件契约（AGENTS.md）。
- 每个任务独立提交；提交信息用 conventional commits。
- 本机为大陆网络：pip 走已配置的清华镜像；npm 已有 node_modules。
- venv 注意：仓库 2026-08-20 从 `Ophira-ai` 更名为 `Open-Acciowork`，旧 venv shebang 失效，已用 `python3.11 -m venv .venv --clear` 重建。

## 现状基线（2026-08-20 实测）

- 后端 `pytest`：53 passed（venv 重建后）。`ruff check`：待跑。
- 前端 `vitest`：10 failed / 5 passed——App.test fixtures 缺 `decisions`/`artifacts` 字段、`startRun` 现在总是带 `mode` 字段、WorkspaceComposer 新增必填 props（`attachedFiles`/`mode`/`onAttachedFilesChange`/`onModeChange`）测试未传、SkillsPage.test 整个对着已删除的 localStorage 旧 UI。
- DeepSeek Provider profile 已配置且预检通过（2026-08-20T11:11Z）；GLM 429 余额不足；无 Anthropic key。
- M2 冒烟：3 个 DeepSeek run 全部 completed（ping / MCP 计算 / Write 简报）。
- 后端 `README.md` 陈旧（还写四子代理架构）。

---

### Task 1: 根 CLAUDE.md

**Files:**
- Create: `CLAUDE.md`（仓库根）

**Interfaces:** 无代码接口。内容必须覆盖：项目一句话定位与 PRD 指针；目录结构（backend/frontend/docs/evals）；红线摘要（指向 AGENTS.md §Product boundary/Architecture，不复制全文）；常用命令（venv 重建、backend 起服 / pytest / ruff，frontend dev / test:run / lint / build，Green Path 与合约测试运行方式）；数据与密钥路径警告（`backend/data/` gitignored、`backend/data/secrets/` 永不读取/提交）；验证纪律（改动后跑对应目录的测试；声称完成前给证据）。

- [x] **Step 1:** 写 CLAUDE.md（≤120 行，中文，命令保留英文）。
- [x] **Step 2:** 校对命令逐条真实可跑（与 README/pyproject/package.json 对照）。
- [x] **Step 3:** Commit `docs: add root CLAUDE.md for agent sessions`。

### Task 2: preflight() 单元测试

**Files:**
- Create: `backend/tests/test_preflight.py`

**Interfaces:**
- Consumes: `ClaudeAgentRuntime.preflight(provider_profile_id) -> ProviderPreflightResult`；`monkeypatch.setattr("ophira_backend.sdk_adapter.ClaudeSDKClient", FakeClient)`。
- Produces: 覆盖 6 条路径的测试：成功（skills 含 `ophira-core:global-opportunity-execution` + health 工具被调）；Skill 未发现 → `SKILL_DISCOVERY_FAILED`；health 未调 → `MCP_INIT_FAILED`；ResultMessage is_error → 分类映射；连接异常 → `BASE_URL_UNREACHABLE`；成功/失败均持久化到 registry（`last_verified_at`/`last_error_code`）。

FakeClient 骨架（脚本化消息流，async context manager + `query()` no-op + `receive_response()` 逐条 yield 构造好的 `SystemMessage(subtype="init", data={"session_id":..., "skills":[...]})`、`AssistantMessage(content=[ToolUseBlock(id, "mcp__ophira_preflight__health", {})], model=...)`、`ResultMessage(...)`）。用 `functools.partial`/闭包参数化消息清单。

- [x] **Step 1:** 写 6 个失败测试（先断言现状接口）。
- [x] **Step 2:** `PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_preflight.py -v` 全部通过（预检代码已存在，测试应直接绿；若发现真 bug 则修）。
- [x] **Step 3:** Commit `test: cover preflight success and all failure classifications`。

### Task 3: providers/skills registry 单元测试补齐

**Files:**
- Modify: `backend/tests/test_providers.py`（追加）
- Create: `backend/tests/test_skills.py`

**Produces（新增断言）:**
- providers：`build_worker_env` 清空三凭据后仅注入所选 profile 凭据、全档位模型映射回落 `profile.model`、独立 `CLAUDE_CONFIG_DIR`；vault 文件 0600/目录 0700；`snapshot()` 不含 `secret_configured`/`last_error_message`；`upsert` 保留上次预检结果；`default_profile_id` 偏好 environment、无凭据返回 None；`classify_provider_error` 8 类映射表逐条；`_safe_id` 拒 `../`。
- skills：真实插件目录发现 4 个 skill（namespace=`ophira-core`）；`set_enabled(False)` 持久化且 `runtime_selection()` 变列表；全启用返回 `"all"`；`snapshot()` 含 id/version/content_hash；损坏 frontmatter 抛 ValueError（tmp 插件夹造例）。

- [x] **Step 1:** 写测试。
- [x] **Step 2:** 跑对应文件 pytest 全绿。
- [x] **Step 3:** Commit `test: provider registry env isolation and skill registry discovery`。

### Task 4: 架构静态门禁（§6.1 → L1）

**Files:**
- Create: `backend/tests/test_arch_gates.py`

**Produces:** pytest 即 CI 门禁：
1. 扫描 `backend/src/ophira_backend/*.py` 的 import 行，禁止 `openai`、`langchain`、`langgraph`、`dify`、`anthropic`（裸 client；`claude_agent_sdk` 允许）。
2. 依赖声明含 `claude-agent-sdk==0.2.82`（读 pyproject）。
3. `_build_options` 产物断言：sandbox 三键、`setting_sources==[]`、`strict_mcp_config`、普通模式 `disallowed_tools==["Agent"]` 且 `agents is None`（复用 test_sdk_adapter 的 build_runtime 方式，防回归双保险）。
4. 事件脱敏 canary：发布含 `sk-ant-xxxx`、`Bearer abc`、`api_key=zzz` 的事件后，events.jsonl 原文 0 命中明文。

- [x] **Step 1:** 写 4 组测试并跑绿。
- [x] **Step 2:** Commit `test: architecture static gates as L1 checks`。

### Task 5: Green Path harness 模块 + 离线验证器测试

**Files:**
- Create: `backend/src/ophira_backend/greenpath.py`
- Create: `backend/tests/test_greenpath_verifier.py`
- Create: `backend/fixtures/greenpath/quotes.csv`、`backend/fixtures/greenpath/constraints.md`

**Interfaces:**
- `GREEN_PATH_PROMPT: str` 综合任务提示词（中文），要求主 Agent：读 uploads 下两份 fixture → WebSearch/WebFetch 至少一次并留 URL → 自主触发 skill（提示业务语境而非点名）→ 调 `calculate_landed_cost` 或 `calculate_unit_economics` → 用 Bash 做一步数据处理 → Write `greenpath-report.md` → 再 Edit 修订 → `record_evidence`（带 source_url）+ `record_decision` → 最后执行标记命令 `echo OPHIRA_DENY_MARKER`（harness 会拒绝一次，验证 deny 后继续收尾）。
- `class PermissionPolicy`: `decide(tool_name, tool_input) -> 'allow_once'|'deny'` —— 输入含 `OPHIRA_DENY_MARKER` 的 Bash 拒一次（记录后放行第二次不需要：Agent 被告知被拒后直接收尾）。
- `async def drive_run(app, project_id, prompt, *, provider_id, resume_session_id=None, policy, timeout_s=900) -> RunRecord`：经 httpx ASGITransport POST /runs，轮询 ledger 事件，实时用 policy 经 `POST /api/runs/{run_id}/permissions/{request_id}` 解决权限，直到终态。
- `def verify_events(events: list[EventEnvelope], *, expect_deny: bool) -> GreenPathVerdict`：dataclass 字段 `tool_coverage: dict[str,bool]`（read/web/skill/mcp/bash/write/edit）、`subagent_events: int`（必须 0）、`permission_flow_ok: bool`（requested→resolved 含一次 deny）、`sequence_contiguous: bool`、`is_error: bool`、`served_model: str|None`、`missing: list[str]`。Skill 触发的判定：`tool.started/planned` 中 `tool_name == "Skill"` 或 tool_input 含 `ophira-core:` 命名空间。
- `def write_bundle(report_dir: Path, round_no, run, verdict, events) -> Path`：写 `summary.json` + `report.md`（工具覆盖矩阵、成本、时长、断言清单；事件均已脱敏仍再过 `redact()`）。

**离线测试:** 手工构造事件序列喂 `verify_events`：完整覆盖 → 全 True；含 `subagent.started` → 违规；缺 Edit → missing 含 edit；sequence 跳号 → contiguous False。

- [x] **Step 1:** 写 fixtures（合成报价 3 行 CSV + 约束 md）。
- [x] **Step 2:** 写 verify_events 离线测试（红）→ 实现 greenpath.py → 测试绿。
- [x] **Step 3:** Commit `feat: green path harness with offline-verifiable assertions`。

### Task 6: Green Path 真端点 3 连过（DeepSeek）

**Files:**
- Create: `backend/scripts/run_green_path.py`（argparse：`--provider deepseek --rounds 3 --report-dir evals/reports --budget-usd 2`）
- Create: `evals/reports/`（产物）

**Interfaces:** 复用 Task 5 全部；round 2 用 round 1 的 `sdk_session_id` 作 `resume_session_id` 验证会话续接（PRD §6.2"会话恢复续改"）；每轮新建独立 project。SSE 断线续传单独验证：round 3 期间用 httpx 流式 GET `/api/runs/{id}/events` 读 N 条断开，再带 `Last-Event-ID` 重连，断言序号衔接不重不漏。
预算护栏：`OPHIRA_MAX_BUDGET_USD=2`、`OPHIRA_MAX_TURNS=40`、DeepSeek `budget_scale` 维持 profile 值。

- [x] **Step 1:** 写脚本；先 `--rounds 1` 试跑，修 prompt/policy 直到单轮全断言过。
- [x] **Step 2:** 连跑 3 轮全过，bundle 落 `evals/reports/greenpath-<ts>/round-{1,2,3}/`。
- [x] **Step 3:** 检查报告无密钥泄漏后 Commit `feat: green path E2E passes 3 consecutive live rounds on DeepSeek`（含报告）。

### Task 7: Provider 合约测试套件 + DeepSeek 报告

**Files:**
- Create: `backend/tests/contract/__init__.py`、`backend/tests/contract/test_provider_contract.py`
- Create: `backend/scripts/run_provider_contract.py`（跑套件 + 汇总 JSON/MD 报告到 `evals/reports/`）

**Interfaces:** 环境门控：`OPHIRA_CONTRACT_PROVIDER=<profile_id>` 未设则整套 skip。用例矩阵（每例独立小 query()，max_turns≤6、预算≤0.3 USD，直接构造 `ClaudeAgentOptions` 复用 `ProviderRegistry.build_worker_env`）：
1. 基本文本+流式增量；2. 模型身份断言（响应 `AssistantMessage.model` 以配置模型为前缀——DeepSeek 防静默映射）；3. Read→Write→Edit 文件链（临时 cwd）；4. Bash 执行；5. 进程内 MCP 单调用；6. Plugin Skill 发现（SystemMessage.skills）；7. WebSearch 可用性探测（失败记录降级而非 fail——能力矩阵字段）；8. 图片消息块（DeepSeek 预期失败→记录）；9. 错误映射：坏 token 临时 profile → `AUTH_REVOKED_OR_INVALID`。
输出：`evals/reports/provider-contract-<provider>-<date>.md` 逐用例 PASS/FAIL/DEGRADED + 能力矩阵建议值。

- [x] **Step 1:** 写套件（无 env 时 pytest skip 干净）。
- [x] **Step 2:** `OPHIRA_CONTRACT_PROVIDER=deepseek` 真跑，产报告；Anthropic/GLM 无可用凭据照实记"未跑：无凭据/额度"。
- [x] **Step 3:** Commit `test: provider contract suite with live DeepSeek report`。

### Task 8: 前端存量测试债修复（回到全绿基线）

**Files:**
- Modify: `frontend/src/App.test.tsx`（fixtures 补 `decisions: []`、`artifacts: []`、run 补 `mode:'standard'`、`provider_profile_id:null`、`provider_snapshot:null`、`skill_snapshot:[]`、`error_code:null`；resume 断言体加 `mode:'standard'`）
- Modify: `frontend/src/components/WorkspaceComposer.test.tsx`（补必填 props：`attachedFiles={[]}` `mode="standard"` `onAttachedFilesChange` `onModeChange`；run fixtures 同上补field；文件上传交互断言对齐现组件）
- Rewrite: `frontend/src/components/SkillsPage.test.tsx`（`vi.stubGlobal('fetch', ...)` 模拟 `/api/skills` 与 enable 端点；断言渲染 4 skill、搜索过滤、toggle 调 POST 并更新按钮态）

- [x] **Step 1:** 逐文件修复。
- [x] **Step 2:** `npm run test:run` 15/15 绿；`npm run lint` 干净。
- [x] **Step 3:** Commit `test(frontend): repair stale fixtures and rewrite SkillsPage test against live API`。

### Task 9: 设置页 Provider UI + 记忆审批（P0-4a）

**Files:**
- Modify: `frontend/src/components/SettingsPage.tssx → .tsx`（顶部新增 `01 Model providers` 分区，本地偏好分区保留下移）
- Modify: `frontend/src/components/settings-page.css`
- Modify: `frontend/src/components/SettingsPage.test.tsx`（新增 provider 交互测试；保留本地偏好测试）

**Interfaces（全部已存在于 client.ts）:** `listProviders()`、`saveProvider(id, ProviderInput)`、`preflightProvider(id)`、`listMemory()`、`decideMemory(id, decision)`。
UI 要求（PRD P0-4）：Provider 列表（display_name/model/base_url/region/secret_configured/last_verified_at/last_error_code+message/能力矩阵 badges：images/prompt_caching/extended_thinking/tool_search）；编辑表单（id、display_name、base_url、auth_env_name 下拉、model、secret 输入 type=password 留空=不改、region、production_eligible）；**不回显 Key**（只显示"已配置"）；"Run preflight"按钮显示结果（ok→绿 verified 时间；错→error_code+可操作修复文案，即后端 message）；错误修复动作=文案+重新编辑入口。记忆分区：列出 `status=proposed` 项，Approve/Reject 按钮调 decideMemory 并原位更新。
测试（先写红）：mock client 模块（`vi.mock('../api/client')`），断言：列表渲染 DeepSeek profile；secret 输入不预填；保存调 `saveProvider` 且 payload 不含空 secret 键；preflight 成功渲染 verified、失败渲染 error message；proposed memory approve 调 `decideMemory('mem_1','approve')`。

- [x] **Step 1:** 写红测试。
- [x] **Step 2:** 实现分区组件（可拆 `ProviderSettings.tsx` 子组件放同目录）。
- [x] **Step 3:** vitest 绿 + lint。
- [x] **Step 4:** Commit `feat(frontend): real provider management and memory approvals in settings`。

### Task 10: 定时任务页接真后端（P0-4b）

**Files:**
- Modify: `frontend/src/components/ScheduledTasksPage.tsx`
- Modify: `frontend/src/components/scheduled-tasks-page.css`
- Rewrite: `frontend/src/components/ScheduledTasksPage.test.tsx`

**Interfaces（已存在）:** `listSchedules()`、`createSchedule({project_id,prompt,cron?,run_once_at?,timezone,mode?})`、`pauseSchedule/resumeSchedule/runSchedule`、`listProjects()`、`getRun(runId)`。
行为：加载时并行拉 schedules+projects；"My tasks"= 真 Schedule 列表（prompt、cron、timezone、enabled、next_fire_at、last_fire_at；last_run_id 有值时拉 `getRun` 显示最近状态，`failed` 显示 error_code——"失败和凭据失效可见"）；每行 Pause/Resume/Run now；创建对话框：模板带 cadence→cron 映射表（`daily_morning→0 8 * * *`、`daily_evening→0 18 * * *`、`weekly_monday→0 9 * * 1`、`every_six_hours→0 */6 * * *`），timezone 取 `Intl.DateTimeFormat().resolvedOptions().timeZone`，必须选 project（下拉列出现有项目；无项目则提示先去 workspace 创建）；移除 localStorage 草稿与"Draft mode"横幅，状态条改为真连接态（加载失败显示后端不可达）。旧 agent 选择器（demand/supply/…）删除——违反单 Agent 口径。
测试：mock client；断言真实列表渲染（含 next_fire_at 与 last run failed 的 error 可见）、从模板创建调 `createSchedule` 且 cron 映射正确、Pause 调 `pauseSchedule`、Run now 调 `runSchedule`。

- [x] **Step 1:** 写红测试 → 实现 → 绿 + lint。
- [x] **Step 2:** Commit `feat(frontend): scheduled tasks page drives the real backend scheduler`。

### Task 11: evals/ 脚手架（§3.11）

**Files:**
- Create: `evals/README.md`（金字塔三层→本仓落点：L1=pytest（arch gates+canary+确定性）、L2=greenpath harness+基准集、L3=judges（未校准不进门禁）；建设顺序=错误分析先行；运行命令）
- Create: `evals/tasks/{a1-demand-entry,a2-supply-entry,b-product-validation,c-supplier-inquiry}.yaml`（每个：id、scenario、prompt、fixtures 路径、minimum_pass 清单=最低合格输出、assertions=复用 verdict 字段子集）
- Create: `evals/tasks/fixtures/`（各任务合成素材：报价 CSV/竞品链接清单 md/销量表 CSV 等，合成数据标注 synthetic）
- Create: `evals/judges/{evidence-coverage,unsourced-claims,fact-inference-confusion}.md`（judge prompt 草稿，顶部显著标注：未经 gold 校准，不得进入发布门禁）
- Create: `evals/gold/README.md`（标注口径模板：TPR/TNR 定义、门槛留空 `≥ __`、gold 由负责人本人标）
- Create: `evals/failure-modes.md`（开放编码→轴向合并两步法模板 + 空表：id/表现/根因轴/断言归属 L1|L2|L3）
- Create: `backend/scripts/run_l2.py`（读 evals/tasks/*.yaml → drive_run → verify_events + minimum_pass 文件断言 → 汇总报告到 evals/reports/l2-<ts>/；`--task` 过滤、`--provider`）

- [x] **Step 1:** 写全部文件；`run_l2.py --task b-product-validation --dry-run`（不打端点，仅装配校验）通过。
- [x] **Step 2:** Commit `feat: eval pyramid scaffolding with benchmark task set v0`。

### Task 12: CI 工作流

**Files:**
- Create: `.github/workflows/ci.yml`

**Produces:** push/PR 到 main 触发两 job：backend（setup-python 3.11 → `pip install -e './backend[dev]'` → `ruff check backend/src backend/tests` → `pytest backend/tests`，live/contract 用例因无 env 自动 skip）；frontend（setup-node 20 → `npm ci` → `npm run lint` → `npm run test:run` → `npm run build`，working-directory frontend）。

- [x] **Step 1:** 写 workflow；本地把等价命令跑一遍绿（CI 语法无法本地验证，命令逐条验证）。
- [x] **Step 2:** Commit `ci: static gates, tests, lint, and build on push`。

### Task 13: 文档收口

**Files:**
- Rewrite: `backend/README.md`（单主 Agent 现实、venv 重建命令、测试/合约/GreenPath 运行方式）
- Modify: `README.md`（Verify 段补 backend 测试命令与 venv 重建注意）
- Check/Modify: `docs/backend-api.md`（照对 main.py 现有 30 端点，过时处修正——只修事实错误不扩写）

- [x] **Step 1:** 逐文件修订。
- [x] **Step 2:** Commit `docs: align READMEs and API doc with single-agent reality`。

### Task 14: 终验、tag、push、汇报

- [x] **Step 1:** 全量验证：`ruff check backend/src backend/tests`；`PYTHONPATH=backend/src .venv/bin/pytest backend/tests`（预期 >70 passed，live skip）；`npm run test:run`（预期全部绿）；`npm run lint`；`npm run build`。
- [x] **Step 2:** `git tag phase0-complete` 于最终提交。
- [x] **Step 3:** `git push origin main --tags`（失败则报告认证问题，不阻塞）。
- [x] **Step 4:** 汇报：Phase 0 出口清单逐项证据（PRD §6.2/§6.3/§6.6 对照）、成本合计、遗留项（FR-23 降级、Anthropic/GLM 合约缺凭据、judge 待 gold 校准、门槛数值待基线填定）。更新 auto-memory。

## Self-Review

- Spec coverage：P0 两个 ✗（定时前端打通=Task 10；Green Path E2E+合约=Task 6/7）✔；P0-3 单测=Task 2/3 ✔；P0-4 Provider UI=Task 9 ✔；§3.11=Task 11（+6 复用）✔；§6.1 CI=Task 4+12 ✔；CLAUDE.md=Task 1 ✔；P0-0 提交+tag=各任务提交+Task 14 tag ✔。FR-23 检索降级：PRD §5 归 Phase 1，明确遗留。记忆审批 UI（P0 △）=Task 9 ✔。
- Placeholder scan：无 TBD；judge 门槛留空是 PRD 明文要求（非占位符）。
- Type consistency：`drive_run`/`verify_events`/`write_bundle` 签名在 Task 5/6/11 一致；client.ts 函数名与 Task 9/10 一致（已照抄现源码）。

---

## 追加批次 A（2026-08-20 用户拍板）：P0 残余清零

### Task A1: FR-10 证据失效联动 + 证据查询 API
- Modify: `storage.py`（`update_evidence_status` 传播到 Decision：stale/removed→追加 `invalidated_by_evidence_ids`+ACTIVE→NEEDS_REVIEW；恢复 active→反向清理；`search_evidence` 下沉复用）、`ledger_tools.py`（新 MCP 工具 `update_evidence_status`，权限门控；`search_evidence` 改调 storage）、`main.py`（`POST /api/projects/{pid}/evidence/{eid}/status`、`GET /api/projects/{pid}/evidence`）、`models.py`（`EvidenceStatusChange` 响应体）
- [x] 测试先行：storage 传播矩阵（置疑/恢复/多决策/无关决策）、API 两端点、MCP 工具注册
- [x] Commit

### Task A2: FR-23 检索 MCP 降级（ophira_web）
- Create: `web_tools.py`（`fetch_page_snapshot`：SSRF 防护【私网/环回/link-local/metadata/非 http(s) 拒绝、逐跳重验、限体积】+ 文本抽取；`search_public_web`：可插拔检索 API【serper/tavily/brave】，未配置时返回结构化 SEARCH_API_NOT_CONFIGURED 而非伪造结果）
- Modify: `config.py`（search provider/key/限额）、`constants.py`、`ledger_tools.build_mcp_servers`（6 服务）、`.env.example`、`models.py` ProviderCapabilities 增 `native_web_search/native_web_fetch`、ProviderSettings 徽章"Web 能力等级"、greenpath 验证器认可 `mcp__ophira_web__*` 为降级 web 覆盖
- [x] 测试：SSRF 矩阵、无 key 结构化错、mock 检索、体积上限；sdk_adapter 服务器集合断言更新
- [x] Commit

### Task A3: §4.15 其余待补 API
- `POST /api/skills/{id}/test`：确定性包检（frontmatter/引用文件存在/内容哈希/密钥扫描/危险命令扫描/allowed-tools 不生效警告）→ SkillTestResult
- Connector 最小注册表（file-backed，§4.11 字段）+ `GET /api/connectors` + `POST /api/connectors/{id}/health-check`（secret 存在性+可选连通探测；不伪造集成）
- `PATCH /api/schedules/{id}`（ScheduleUpdate 全可选字段+触发器互斥校验+next_fire 重算）
- [x] 各自测试 + Commit

### Task A4: 前端——证据抽屉 + Skill 测试入口
- client.ts 增量（listProjectEvidence/setEvidenceStatus/testSkill/patchSchedule/listConnectors）；useOpportunityRuntime 暴露 refreshActiveProject
- `EvidenceDrawer.tsx`：kind 徽章（观测事实/用户陈述/社区信号/推断）、来源链接/路径、retrieved_at/observed_at、置信度、freshness_note、status 操作（置疑/移除/恢复→调 API→提示 N 条结论需复核）、筛选；Decisions 区 needs_review 徽章
- SkillsPage 每卡 "Test" 按钮渲染检查结果
- [x] vitest 先行 + lint + build + Commit
### Task A5: 终验
- [x] 全量测试两端；单轮 Green Path 真端点回归（6 MCP 服务）；docs/backend-api.md 增补；push；memory 更新；对照表汇报
