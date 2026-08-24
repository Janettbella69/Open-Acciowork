# Agent Harness 教材全量对照审计（2026-08-24）

> 对照物：《Agent Harness 教材》（训练营 2026-01~08 实录，14 章 + 手册判据总表）。
> 方法：按手册"判据总表"逐条核对本仓库代码/文档/评测资产，全部给出证据位置。
> 结论标记：✅ 符合 · △ 部分符合/有保留 · ❌ 违背 · 🔧 本次已修（78bb7cf）· — 不适用。
> 修正决策见 `design-decisions-2026-08-23.md`；三条不变量为判断源头：
> **一台电脑**（文件系统+bash，能读能写能执行）· **一个循环**（无状态机，模型决定下一步与何时停）· **一个窗口**（上下文是智能唯一载体，有限，一个 Agent 只有一个）。

## 三条不变量

| 不变量 | 结论 | 证据 |
|---|---|---|
| 一台电脑 | ✅（🔧强化） | `tools={"preset":"claude_code"}` 全套内置工具（sdk_adapter.py）；storage 全 JSON/JSONL 文件；沙箱内 Bash 全量自动批准（SDK 原生 `autoAllowBashIfSandboxed=True`，本次修）；账本写入/对外发送经人审=教材 09 章合法形态 |
| 一个循环 | ✅（🔧修复） | SDK 原生循环、无状态机、无意图路由；`max_turns` 默认 None（config.py，本次修），预算是唯一保险丝 |
| 一个窗口 | ✅ | 单主 Agent；`disallowed_tools=["Agent"]` 常规模式禁委派；仅 Deep Research 注入 SDK 原生只读子代理（教材四例外之一） |

## 架构层（教材 01–03 章判据）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| Workflow 还是 Agent？有追问→Agent | ✅ | 产品核心即多轮追问对话（Workspace 对话线程 + `resume_session_id` 续会话） |
| 值不值得做 V3？Dify 两条工作流搞得定就换题 | ✅ | 跨境机会验证 = 证据链+多工具+追问+不确定性，远超工作流可枚举范围 |
| 用什么模型？基线 = Sonnet 5 / GLM 5.2 / DeepSeek | ✅ | DeepSeek V4-Pro 主力、Kimi K3 vision 席位、GLM 备用（docs/model-providers.md），全部基线及以上 |
| 前置小模型/代码分类能撑多久 | — | 没有前置分类器（正确的没有） |
| 平台有没有真正 Harness？能不能为所欲为 | ✅ | SDK Harness，沙箱内全能力；workspace 围栏与敏感路径黑名单是安全边界不是能力裁剪 |
| 我的产品要不要 Harness？在 Claude Code 里说一遍看它行不行 | ✅ | L2 基准 4/4 PASS 两轮（evals/reports/l2-*），已用真跑验证 |
| 要不要 Skill？先要 Harness | ✅ | 有 Harness 在先，Skill 经本地 Plugin 装载（backend/ophira-capabilities/） |
| 国产模型接对了吗？base url 带 anthropic 字样 | ✅ | 4 个 provider profile 全部 `…/anthropic` 官方兼容端点（backend/data/providers/*.json、docs/model-providers.md） |
| 传统代码只有四个合法位置 | ✅ | FastAPI/存储/Provider 体系=基础设施；prompt 装配=Agent 启动前；hook=副作用；PermissionBroker=暂写人审。无循环中间写死 |
| 每条限制是"前人的坑"还是"我担心它做不好" | ✅ | Skill 全是经验口径（"不要强制套入固定平台/固定四阶段/固定工具顺序"），约束写成经验 |
| 听 AI 的架构建议保留约束？ | — | 流程判据；本仓库以 CLAUDE.md 红线 + arch_gates 测试为准 |
| 跑通了要不要继续优化 | ✅ | 本次全部改动均有明确优化点（FM-01/02 打断、cache 前缀），无为优化而优化 |
| 场景大模型能解吗（博士判据） | ✅ | 跨境商业尽调=正常商科常识+可给定口径 |
| 该不该用 AI | ✅ | 非是非题；自然语言+非结构化输入为主 |

## 控制流（04 章）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| 几个 Agent？ | ✅ | 1 个主 Agent；无 host 层/总控/auditor/worker |
| 不同阶段拆 Agent？不拆，按阶段加载 Skill | ✅ | 需求/供给/资金/销售是 Skill 视角与证据标签，非强制 Agent（AGENTS.md Product boundary） |
| 多 Agent 四个合法例外 | ✅ | 仅 Deep Research（例外之二），且用户显式开启才注入 |
| 子 Agent 只做独立任务（实习生不是项目经理） | ✅ | 3 个只读研究子代理只做来源研究；主 Agent 唯一写者、负责汇总（_system_append 模式规则） |
| 多用户要不要子 Agent？不要，每用户一个 session | ✅ | 每 run 一 session，天然隔离 |
| 并发/DR 用 SDK 原生，不自己编排 | ✅ | `agents=build_deep_research_agents()`，无自造通信机制 |
| 数字员工团队 = Plugin（N Skill + N MCP + 名字） | ✅ | ophira-capabilities plugin 即此形态 |
| Codex 有没有偷塞 Agent？调了 AI 就是错 | ✅ | 全仓审计：唯一 LLM 调用点是 sdk_adapter 的 SDK query/preflight，无第二个 AI 调用 |
| 架构走形？只留一份架构文档 | ✅ | 权威规格唯一（PRD v2，CLAUDE.md 明示）；v1 PRD 留档但 CLAUDE.md 指向 v2 |

## 工具（05–06 章）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| 必须保留 bash/read/write 兜底 | ✅🔧 | preset 全保留；沙箱内 Bash 全量自动批准（SDK 原生默认，本次恢复），零打断 |
| 不造记忆系统，记忆就是文件 | ✅ | FileMemoryProvider（JSONL+MEMORY.md 镜像），无向量库/embedding |
| Agent 互通用文件/数据库，不做 A2A | ✅ | 定时运行↔收件箱↔账本全走文件 |
| 本地还是云端电脑 | ✅ | 本地后端+项目 workspace；无登录态强需求 |
| MCP 两判据：跨系统才封 / 交叉编排写 SQL | ✅ | 账本=单一结构化业务系统（写入挂去重/失效联动/审计副作用，正是"业务动作"）；无多表交叉编排需求（JSON 账本非 SQL 数据平台）。边界已记录 |
| 分析/解释/导出不封 MCP | ✅ | 无此类 MCP；4 个确定性计算工具是"人类调好的关键模块"例外，理由与新增门槛在 design-decisions §D |
| AI 原生能力不封 MCP | ✅ | 无会议纪要/摘要/翻译类 MCP |
| MCP 数量：≤20 最好，≤30 算多 | △ | 26 工具/7 server——在"算多"区间；每个都有审计副作用或跨包络语义，暂不合并，>30 时强制复审 |
| 按业务域切 | ✅ | ledger/market/supplier/customer/commerce_math/web/automation |
| >40 上 CLI 或搜索式加载 | — | 26<40 未触发 |
| MCP 还是参考代码？规整增删改查→MCP | ✅ | 账本接口规整稳定，符合教材"项目管理类保留 MCP"同款判据 |
| MCP 数量是系统真实度体温计 | ✅ | 真后端真接口，调错会报错（Provider 合约测试 8 用例） |
| RAG？从零第一版不做 | ✅ | 未做 RAG；workspace Markdown 直读 |
| 知识库形态 = 有目录的 Markdown | ✅ | agent core dir（IDENTITY/AGENTS/TOOLS/MEMORY/USER/SOUL.md）+ workspace |
| 文档怎么进来？PDF/图片 → PaddleOCR-VL | ❌ | **缺口**：后端零 PDF/OCR 处理，当前输入限 md/csv/自然语言。待办（见文末） |
| 〔补〕工具描述是 Prompt、错误可指导自纠 | ✅ | 工具描述声明"不代替商业判断"；ophira_web 未配 key 返回结构化错误不伪造 |

## 上下文（07–08 章）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| 要不要 Skill？常识业务→不要 | ✅ | 跨境尽调口径非常识 → 4 个 Skill，未过度 |
| Skill 写什么：裂缝/注意事项/按图索骥 | ✅ | 证据分级铁律、代理信号口径、确定性计算引导；不写模型本来就会的 |
| Skill 几个？拆太碎？ | ✅ | 4 个，按业务视角 |
| Skill 还是拼 System Prompt？延迟敏感→拼 | ✅ | 报告类任务延迟不敏感，渐进加载合理 |
| 表现不好先查 Skill 误导 | ✅ | FM 工作簿开放编码承担此职能 |
| 怎么写 Skill：抓竞品/从日志总结 | ✅ | competitive-reverse-engineering-2026-08-21.md（竞品逆向）；FM→提示词修正闭环已跑一轮 |
| System Prompt：你是谁/有什么/注意事项/剩下自己来 | ✅ | SECURITY_PREAMBLE→身份→工具→注意事项→松绑句式（"自主判断合适的方法、工具、顺序"，agent_core.py） |
| 背景不可替代 | ✅ | IDENTITY/USER/SOUL + 项目 objective 全给 |
| 六层上下文 | △ | 覆盖①表结构②人工标注（Skill 口径）⑤记忆⑥实时查询；③代码反推④文档库对自有账本系统不适用 |
| schema 之外的语义从哪来 | ✅ | 字段语义在工具描述与 Skill 口径里 |
| 分层记忆抽象？无实时性压力不做 | ✅ | 未做（正确的没做） |
| Handoff ≠ 记忆；跟着 SDK 走 | ✅ | SDK resume + 记忆提案制，无自造 |

## 人机协作（09–10 章）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| Hook 还是代码门禁？副作用→Hook，替判断→删 | ✅ | 全部 hook 只管副作用/安全：事件发布、路径围栏、敏感路径黑名单、Bash 分级。无业务判断代码 |
| 防写坏数据 = 暂写 + 人确认 | ✅ | 记忆提案制、账本写入 ask、询价外发先展示待批（templates.py） |
| 什么时候全自动？可逆+不碰生产数据 | ✅ | §4.10 包络原则：只读自动、收件箱投递 auto、跨包络承诺 ask——与教材判据同构 |
| Agent 不确定 → AskUserQuestion 选择题 | △ | claude_code preset 自带且 SDK 保证落 can_use_tool（permissions.md:81）；前端对 question+options 卡片的渲染未专测 |
| 什么时候问人 | ✅ | prompt："只有缺少关键决策、权限或会造成真实外部影响时才询问用户" |
| 要不要沙盒？教材：不要 | ✅🔧 | 教材反对的是"自造沙箱/什么都读不到"的伪 Harness；本仓库是 SDK 原生沙箱，且本次起沙箱内 Bash 全量自动批准——沙箱零打断、只围副作用（"能不能为所欲为"判据通过） |
| 第一版必须 chat | ✅ | Workspace 对话线程为主体，自然语言输入 |
| 不是所有产品住对话框 | ✅ | schedules/skills/settings 独立页（甘特图判据同款常识） |
| 怎么嵌 GUI？先万能再嵌入 | ✅ | chat 先行，证据抽屉后挂 |
| 结构化输入没控住会崩 | ✅ | 输入是自然语言+文件，无结构化表单切碎 |
| 太慢怎么办 | ✅ | SSE 流式过程让等待可见；无"简洁/深度"开关 |
| 输出先过程后结果 | ✅ | conversation-process（reasoning 摘要+工具事件时间流）在正文前（WorkspaceComposer.tsx），可展开 |

## 底座（11–13 章）

| 判据 | 结论 | 证据/说明 |
|---|---|---|
| 日志第一版就带 | ✅ | 27 种 SSE 事件 + 三级日志（manifest/summary/JSONL）Phase 0 起就有 |
| 日志写给 AI 读 | ✅ | digest_run.py trace 摘要工具；JSONL 结构化 |
| 慢的四步诊断可执行 | ✅ | 事件带时间戳、usage/成本入 summary |
| 架构走形定期确认 | ✅ | CLAUDE.md 红线 + test_arch_gates.py 自动断言——比教材"定期重发黄金 prompt"更强（机器化） |
| 看过 hello 那一刻加载了什么吗 | △ | 未实测拼接后的启动上下文体积（26 工具说明每轮在场）。待办：跑一次原生拼接检查 |
| AI 浏览器端到端验证 | △ | Green Path = API 级端到端真任务（3 连过）；浏览器级验证未做。待办 |
| 有正确答案→代码评 | ✅ | L2 机器断言（工具覆盖/账本写入/产物存在），judge 只管需判断力的维度 |
| 评测 Agent 导演视角 | ✅ | failure-modes.md 跨 4 trace 开放编码找规律；fix-validation 新旧版本对比 |
| 二元还是三档 | ✅ | 逐条二元 + 自然语言 reason（教材"两个都做"） |
| 评测 Agent 要自由 | ✅ | 开放编码不受平台接口限制，直读原始 trace |
| 不用假数据 | △ | fixtures 如实标注 "synthetic sample compiled by user; proxy signal only"——教材允许"没有真数据时造尽可能真的"；真用户数据进来后替换 |
| 改 Skill/Prompt 跑回归 | ✅ | 基线→复测两轮 L2 对比已跑通（FM-02 验证有效即产物） |
| 数据反馈闭环 | 🔧立项 | P2-feedback-loop-spec.md（本次 E）：三入口两回流，人审唯一通路 |
| 交接 IT 架构保护 | — | 未到交接阶段；架构保护条款已在 CLAUDE.md |
| cache 前缀稳定 | ✅🔧 | preset+append 固定在前；每 run 变化的 trace_id 挪至 prompt 末尾（本次 B） |
| 不造插件轮子 | ✅ | SDK 原生 plugin 机制 |
| 通用做不过大厂→垂直 | ✅ | 垂直跨境小 B，非通用 workbuddy |

## 第二轮：全文件通读校正（2026-08-24，18d34ce）

判据抽查之后又对源码做了逐文件通读（sdk_adapter/agent_core/templates/permissions/runs/
ledger_tools/web_tools/automation_tools/greenpath/scheduler/events/main + 全部 4 个 SKILL.md）：

- **修正 3 处 prompt 层旧口径**（Agent 每轮都读，影响最大）：SECURITY_PREAMBLE"执行 Shell
  须批准"→"绕过沙箱须批准"；DEFAULT_AGENT_PROMPT 压制 Bash 的旧条款→教材万能兜底句
  （"Skill/MCP 没覆盖的任务直接用 Bash/Write/Edit 写代码解决"）；_GENERAL_TOOLS"Bash 每次
  执行都需用户批准"→沙箱内自动放行 + 兜底句。
- **其余全部读净**：唯一 AI 调用点在 sdk_adapter（全仓 grep 证明无偷塞）；事件流 1:1 映射
  原始 SDK 流、redact 只脱敏不重组（原始日志不封装）；scheduler/main 把 prompt 原样直达
  主 Agent（无中间改写）；权限 broker 纯人把关；工具描述全部声明"不代替商业判断"、错误
  结构化可自纠；4 个 Skill 全是经验口径、外发点人把关。
- 磁盘上无已建 Agent（backend/data/agents 不存在），core 文件无需迁移。

## 汇总与反模式自查

- **本次已修（78bb7cf + 后续提交）**：max_turns 上限（→None，不设轮数上限）、Bash 强制 ask（→SDK 原生沙箱内全量自动批准，中间白名单层随"教材为最终权威"拍板删除）、trace_id 位置（→prompt 末尾）、评测归属口径、计算 MCP 理由记录、反馈闭环立项、CLAUDE.md 补 OCR 规则与端到端验证纪律。
- **有意保留的偏离**：无——2026-08-24 用户拍板"一切以教材为最终"，沙箱改为原生零打断形态后不再构成偏离。
- **开放缺口（4，按优先级）**：
  1. PDF/图片入口零处理（教材：统一 PaddleOCR-VL 转 Markdown）——影响真实用户上传发票/报价单场景；
  2. 数据反馈闭环（已立项 P2，前置=gold 本人标注）；
  3. "hello 那一刻"启动上下文体积未实测；
  4. 浏览器级端到端验证未做（现为 API 级）。
- **反模式索引自查命中数**：修复前 3 条（轮数上限/全量 ask 属"加闸门"变体/随机 ID 位置），修复后开放命中 ≤2（OCR 缺口、反馈闭环在建）——教材口径"中 5 条以内正常"。

下一轮验证：跑 L2 复测，对照基线 `l2-20260821T023627Z` 验证打断密度与 FM-01 是否显著下降（预期由只读 Bash 放行直接消除开场 `ls` 打断）。
