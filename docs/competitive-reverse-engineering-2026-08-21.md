# 竞品逆向分析报告（6 个目标）· 2026-08-21

- 方法：教材 1.3「把安装包丢给 AI 拆开」。4 个本地 Electron app 用 `@electron/asar extract` 解包后 grep 取证；
  2 个开源项目读源码与官方文档原文。**只做原理理解与技术可行性评估，未搬运任何竞品源代码。**
- 证据口径：每条结论附文件路径或原文串。头号断言（Accio 的 ADK 底座、Claude Desktop 的 SDK 版本）已由本人二次复核。
- 本报告只做分析，不含任何对本仓代码的改动。

## 0. 目标清单与身份实锤

| 目标 | 真身 | 版本/规模 | 证据 |
|---|---|---|---|
| Accio.app | **阿里巴巴国际站（ICBU）产品，内部代号 Phoenix** | v0.29.2 beta，解包 12,118 文件/191MB | `package.json` name=`@phoenix/desktop`；网关 `phoenix-gw.alibaba.com`；依赖 `@ali/icbu-domain-transfer` |
| WorkBuddy AI.app | **腾讯 CodeBuddy（代号 genie）的渠道贴牌版** | v5.3.13，asar 283MB | Info.plist 版权 Tencent；`cli/package.json` name=`@genie/agent-cli`、bin=`codebuddy`；`channel-branding/` 内含中国移动/联通贴牌图 |
| Claude.app | Anthropic 官方桌面端 | v1.32885.1，asar 30MB | — |
| ChatGPT.app | **OpenAI 已把桌面与 Codex 内核合并** | v26.803.41515，bundleid=`com.openai.codex` | Electron 壳 213MB + 原生 Rust 内核 `codex` 218MB |
| openclaw/openclaw | 开源个人助手平台 | 386k star，33,313 文件，TS | 官方 docs 原文 |
| nousresearch/hermes-agent | 开源自改进 agent | MIT，9,948 文件，Python | 源码 docstring |

## 1. 三条技术路线的差异（这是本报告的核心）

六家分成三条路线，**没有第四种**：

| | 路线 A：用官方 Agent SDK | 路线 B：用第三方 Agent 框架 | 路线 C：完全自研 loop |
|---|---|---|---|
| 谁在走 | **Claude Desktop**、**OPHIRA** | **Accio**（Google ADK）、**WorkBuddy**（OpenAI Agents SDK） | **Codex**（Rust 内核）、**Hermes**（Python）、**OpenClaw**（TS） |
| 证据 | `package.json`: `@anthropic-ai/claude-agent-sdk@0.3.234` + 灰度 pin `0.3.235-dev.20260817.sha0943baf` | `@ali/accio-adk-ts` 依赖 `@google/adk ^0.4.0`+`@google/genai ^1.43.0`；WorkBuddy 依赖 `@openai/agents 0.5.2`，bundle 内 `handoff` 命中 110 次 | Codex `codex app-server` RPC + Rust 内核；Hermes `agent/conversation_loop.py` 单函数约 3,900 行；OpenClaw `runEmbeddedAgent` |
| loop 归谁 | SDK（官方维护） | 框架（第三方维护） | 自己（全部自维护） |
| 模型绑定 | 深绑 Claude 生态 | 框架中立 + 自建网关多厂路由 | 完全自由，每家写 adapter |
| 升级成本 | 跟 SDK 版本走，低 | 跟框架版本走，中 | 无外部约束，但全部自担 |
| 上限 | 受 SDK 能力边界约束 | 受框架能力边界约束 | 无上限 |

**代价对照（关键）**：

- **路线 A 的代价**：能力边界由 SDK 决定；SDK 一升级你要跟。**红利**：官方把权限、MCP、hooks、sandbox、session 全做好了——Claude Desktop 自己就是最好的证明，它**没有重写任何一块 agent 机制**，权限模型就是 SDK 的 `permissionMode` 四态 + `canUseTool` 契约原样用。
- **路线 B 的代价**：要自建云网关才能发挥框架的中立性。Accio 为此建了多区域 `phoenix-gw.alibaba.com`（prod/us/sg/de/cn）+ 设备指纹 umid + 请求签名 urlSign + 账号体系；WorkBuddy 建了 `copilot.tencent.com` + TencentCloud SSO + credits 计费。**这层是大厂体量才划算的投入。**
- **路线 C 的代价**：Hermes 的 loop 单函数 3,900 行、Codex 的内核 218MB 二进制——全部要自己维护、自己修 bug、自己跟模型 API 变更。**红利**：能做 SDK 做不到的事（Codex 的 Seatbelt 逐命令沙箱、Code Mode）。

### 一个反直觉的发现：多模型不必然要自建网关

Accio 走的是"云网关代管多厂"：`model-catalog.json` 里每个模型带 `group` 字段（实测取值只有 `google`/`azure`/`dashscope` 三个），默认模型是 `gemini-3-flash-preview`，Claude 的 `claude-sonnet-4-6`/`claude-opus-4-6` 走 group=`google`（即经 Vertex）。产品只传 `selectModelName`，云网关按 group 路由。

**OPHIRA 用更省的方式达到了同样的多模型效果**：只走各厂商官方 Anthropic 兼容端点，`build_worker_env` 改写 `ANTHROPIC_BASE_URL` + 注入唯一凭据，SDK 一行不改。代价是只能接"提供 Anthropic 兼容端点"的厂商——但国产主力（DeepSeek/Kimi/GLM）都提供。**这是我们已经做对的一件事，不需要改。**

## 2. 六维度横向对照（OPHIRA 视角）

| 维度 | Accio | WorkBuddy | Claude Desktop | Codex | OpenClaw | Hermes | **OPHIRA 现状** |
|---|---|---|---|---|---|---|---|
| **结论级证据链** | ❌ 白地。`evidence`(373) 实为模型流诊断、`provenance` 实为浏览器会话来源、`ledger` 实为插件安装账本；citation/grounding 主包各约 2 次 | ❌ 提示词纪律，非硬账本：citation(566) 靠模型自觉 + "请自行校验"提醒 | ➖ 非其定位 | ➖ | 🟡 记忆有 provenance，结论无 | ➖ | ✅ **FileLedger 硬账本**（evidence/hypothesis/decision 结构化，FR-10 失效联动） |
| **权限** | ✅ 强：逐工具 `requiresPermission`+`permissions/audit.jsonl`+**IM 群决策审批** | ✅ Claude-Code 同级 + fs/network 守卫 | ✅ SDK 四态+`canUseTool`+**持久化 consent** | ✅✅ **二维矩阵**(sandbox×approval)+命名 profile 层叠+模型可申请提权 | ✅ safeBins+禁解释器白名单 | ✅ 幂等/变更二分护栏 | ✅ 双层防御，**但无持久化决策**（见差距 D1） |
| **渠道/IM** | ✅ Feishu(99)/DingTalk(88)/Lark(70)/Telegram(55)/Discord(39)，**WhatsApp/Slack 各仅 1 次** | ✅✅ 9+ 渠道适配器三件套 | ➖ | ➖（私有 RPC） | ✅ 29+ 渠道 | ✅ 6 渠道 + **投递义务账本** | ❌ 未开始（FR-24 在 Phase 3） |
| **主动服务** | ✅ CronAgentRunner+派发队列 | ✅ SQLite automations+rrule | 🟡 | ✅ rrule+APNs | ✅ heartbeat=主会话周期 turn，默认 30m | ✅ 文件锁 tick，60s | ✅ 后端已完成（收件箱+意图+防唠叨预算） |
| **记忆** | 🟡 memory.md + 本地索引，无重型向量库 | ✅ 分层 + SOUL/IDENTITY/USER/BOOTSTRAP 人格文件 | ✅ | ✅✅ 二阶段 consolidate（抽取模型+固化模型各一） | ✅✅ 写路径即安全边界 | ✅ 自改进闭环 | ✅ 提案-批准制（Agent 只能提案） |
| **沙箱** | ✅ Seccomp/RestrictedToken + 自带 pinned python/node/git | ✅ 直接用 `@anthropic-ai/sandbox-runtime` | ✅ SDK sandbox + **凭据脱敏出网代理** | ✅✅ 逐命令 Seatbelt/.sbpl + 网络白名单代理 | ✅ | 🟡 | ✅ SDK sandbox 强制开启（红线） |

**结论：OPHIRA 唯一的结构性优势是"结论级证据链"，而这恰是头号对标 Accio 的白地。** 两家中国大厂产品都把"证据"做成了提示词纪律或可观测性，没有做成不可绕过的账本。

## 3. 方案光谱：从最省事到最彻底

针对"我该走哪条路"，按投入排序（技术栈前提：Python 3.11 + Claude Agent SDK 0.2.82 + FastAPI + React）：

| 档 | 做什么 | 投入 | 得到 | 代价/风险 |
|---|---|---|---|---|
| **0. 什么都不改** | 维持现状 | 0 | Phase 0 资产完整 | FM-02 剩余打断、凭据出网无防护、SDK 落后 61 补丁版 |
| **1. 补 SDK 官方契约**（推荐起点） | `can_use_tool` 返回 `updated_permissions`/`suggestions` 实现持久化批准（**Bash 除外，守红线**） | ~半天 | 直接砍 FM-02 剩余打断 | 需拍板"哪些工具可持久化" |
| **2. 升级 SDK + 双 pin 灰度** | 0.2.82→0.2.143，仿 Claude Desktop 同时 pin 稳定版+候选版，合约测试双跑 | ~1 天 + 真端点成本 | 61 个补丁版的修复；升级风险可控 | 必须先过 Provider 合约测试（红线） |
| **3. 凭据脱敏出网** | 仿 Claude Desktop `sandbox.credentials.files`：沙箱内假 token，出网换回 | ~2-3 天 | 堵住"agent 外泄 provider key"这一真实缺口 | 要引入 egress 代理层 |
| **4. 一条 IM 渠道（FR-24）** | 飞书优先（我们有 lark-personal MCP 凭据）；仿 Hermes 的**投递义务账本**四态 | ~1-2 周 | 品类 table stakes | 每条渠道各有鉴权/限流/合规 |
| **5. 权限升维** | 仿 Codex 的 sandbox×approval 二维矩阵 + 命名 profile | ~1-2 周 | 表达力强一个量级，求职作品可讲 | 改动权限核心，需重跑全部权限测试 |
| **6. 自建模型网关** | 仿 Accio/WorkBuddy 建多厂路由网关 | ~1-2 月 | 跨厂 failover + 成本路由 | **不建议**：阿里/腾讯体量才划算；我们已用官方兼容端点达到多模型 |

**我的推荐：1 → 2 → 4，跳过 6。** 理由：档 1/2 是绿区（改错代价小）且直接消化已知失败模式；档 4 是 PRD 既定 Phase 3 且我们已有飞书凭据；档 6 是教材 2.6 说的"规模不匹配的大厂标配"。

## 4. 他们踩过、我们一定也会踩的坑

从各家的护栏代码反推出的"血泪"（每条都是他们花代价买的）：

1. **渠道投递会无声丢失**——Hermes `gateway/delivery_ledger.py` 原话："回复已经烧了 token，但只存在于一个 Python 局部变量里，finalize 和平台 ACK 之间崩溃就无声丢失"（附 issue #58818/#41696/#63695）。解法：pending→attempting→delivered/failed 四态持久化 + 启动扫描重投。**我们做 FR-24 时第一天就要有这个，不要等丢了再补。**
2. **自由 loop 会死循环/漂移**——Accio 给工作流套了 `validateMaxIterations`（"maxIterations is required for loops"）、`requireProgress`，A2A 另有 `maxTurns:128/maxNestingDepth:3/maxTimeoutSeconds:43200/maxRequests:20`。**我们的 Deep Research 子代理和定时巡检需要同类护栏。**
3. **把解释器放进白名单等于没有白名单**——OpenClaw 原文警告：不要把 `python3/node/ruby/bash` 加进 safeBins，"能求值代码、能执行子命令的一律走显式审批"。**这条正是我们那个"sandbox 绕过 Bash 权限"bug 的教训的通用版。**
4. **自写记忆会漂移/被投毒**——WorkBuddy 让 Agent 自己写 `SOUL.md`/`IDENTITY.md`，配了护栏（改 SOUL 要告知用户、装 skill 先过 P0/P1/P2 安全审计）；OpenClaw 更狠：**"写路径就是安全边界——内容级扫描抓不住投毒记忆，所以写入时强制 provenance、结构化门禁控制晋升，而不是事后检测"**。**我们的提案-批准制方向正确，但 §4.16 记忆蒸馏上线前要补 provenance 字段。**
5. **技能不维护就是负债**——WorkBuddy 原话 "Unmaintained skills are liabilities, not assets."，配套纪律：用到的 skill 发现缺陷必须**同轮**修。
6. **长会话既贵又误导**——OpenClaw 明确"历史里全是旧版工具的调用记录，会误导它"，所以更新 SKILL 后必须开新会话。**这对我们的 handoff 纪律是佐证。**
7. **合规层不可照抄**——WorkBuddy 的 content-policy 写死了"假设用户是中国人"+ 大陆法律敏感词 + GB/T 规则包。OPHIRA 是跨境出海产品，**需要一套镜像的合规/i18n 层，不能复用**。

## 5. 三个可直接落地的差距（待负责人拍板，本报告不改代码）

| # | 差距 | 证据 | 建议 |
|---|---|---|---|
| **D1** | `can_use_tool` 只返回裸 `PermissionResultAllow()`，未用官方的 `updated_permissions`/`suggestions` 持久化批准 | 我方 `sdk_adapter.py:344`；官方 `docs/claude-agent-sdk/python.md:1143` 明载该机制可持久化规则；Claude Desktop 实测在用（`alwaysAllow`/`allowlist`/`updatedPermissions`） | 对账本类工具启用，**Bash 明确排除**（守"Bash 授权永不缓存"红线） |
| **D2** | SDK 落后：我们 0.2.82，PyPI 最新 0.2.143（同一 0.2.x 线，61 个补丁版） | PyPI API 实查 | 升级 + 仿官方双 pin 灰度；必须先过 Provider 合约测试 |
| **D3** | 无凭据出网防护：`redact()` 只管事件日志，管不住 agent 把真 key 写进网络请求 | Claude Desktop `sandbox.credentials.files`："沙箱内解析照常工作，代理在出网时把整个假 token 换回真的" | Phase 2 候选；先记录为已知缺口 |

## 6. 结论

**这条路走得通，且已被两个大厂产品（阿里 Accio v0.29.2、腾讯 CodeBuddy v5.3.13）和三个开源项目量产验证。** 骨架共识是：本地/服务端 agent 运行时 + MCP 扩展 + 逐工具权限 + 沙箱执行 + 主动服务 + 分层记忆。

**但不能靠复刻广度赢。** OPHIRA 的位置应该是"同骨架、错位取胜"：

1. **结论级证据链**——头号对标 Accio 的结构性白地（它只治理动作，不为结论溯源），也是我们已经建成的东西；
2. **Claude-native 深度**——路线 A 的红利是官方把权限/MCP/hooks/sandbox 都做好了，Claude Desktop 证明了"不重写任何 agent 机制"是可行且正确的；
3. **eval 体系 + 数据闭环**——六家里没有任何一家把评测体系当产品能力外显；Codex 的"一份 rollout trace 供回放/eval/记忆三用"是唯一接近的，值得借鉴其存储范式。

一个战术空档记录在案：**Accio 的 WhatsApp/Slack 几乎没接**（各仅 1 次命中，而 Feishu/DingTalk/Telegram/Discord 都是几十上百次），而出海小 B 的客户沟通大量在 WhatsApp——这是可正面切入的差异点，但属于 Phase 3 渠道决策，此处仅记录。
