# Anthropic Commerce Agents 全量审读与 Open‑Acciowork 对照

> 审读日期：2026-09-02（America/Los_Angeles）
> Anthropic 文章：[A guide to the anatomy of effective commerce agents](https://claude.com/blog/the-anatomy-of-effective-commerce-agents)
> 参考仓库：[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents)，固定提交 [`fd4d592`](https://github.com/anthropics/commerce-agents/commit/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf)
> Open‑Acciowork 比较基线：[`036ffc6`](https://github.com/Janettbella69/Open-Acciowork/tree/036ffc621e1842661e54dafc62196f075fc9796c)

> **时点声明：**本文中的“目前”“当前工作树”“真正缺失”和建议顺序，只描述
> 2026-09-02 审读时的固定比较基线与当时可见 WIP，不是 2026-09-03 之后的实时
> 项目状态。后续实现不得倒写成当时已经存在；当前能力与剩余边界请以
> [`requirements.md`](requirements.md) 和
> [`OPHIRA-PRD-and-Technical-Architecture-v2.md`](OPHIRA-PRD-and-Technical-Architecture-v2.md)
> 的最新复核为准。文内明确标日期的实施状态框仅是对应日期的增量记录。

## 1. 先说结论

Open‑Acciowork 不需要改造成 Anthropic 仓库的翻版。它的总架构已经走在同一条正确路径上：一个主 Agent 持有完整上下文，Skills 承载长尾流程，确定性工具和权限系统承载事实与副作用，结构化账本承载项目真相。真正需要吸收的是 Anthropic 的四组**模型外契约**：

1. 外部读取先生成服务端签发的来源/对象 ID；展示、决策和写入只接受这些 ID，并由服务端重新补全权威字段。
2. 模型只能 stage；独立的人或政策层 approve/apply；执行前重新读取现状并校验权限、版本、额度和最终状态。
3. UI 是有类型的工具结果，不是模型生成的 Markdown/自定义标签；卡片布局、历史回放和引用关系都进入原生消息/事件协议。
4. 评测直接构造状态快照，断言最终账本、渲染结果和安全不变量；不把固定轨迹跑通或事件数量达标当成任务成功。

目前 Open‑Acciowork 的主要短板不是 Prompt，而是**真实业务适配器、可证明的来源链、可执行评测门禁和生产身份边界**。当前未提交工作树已经在实现 typed presentation、统一不可信输入边界和 commercial stage/review/apply；方向正确，部分设计甚至比参考仓库更严格，但它们还不能计入稳定 HEAD，也没有真实外部 adapter 完成最后一跳。

一句话判断：**保留 Open‑Acciowork 的 SDK-only 主架构；复制参考项目的不变量与测试思想，不复制它的手写 loop、demo IAM、mock backend 和运行时妥协。**

## 2. 审读范围与证据强度

### 2.1 实际覆盖

| 对象 | 覆盖 | 方法 | 未覆盖 |
|---|---:|---|---:|
| 博客正文 | 页面正文第 135–471 行，含标题、全部章节与致谢 | 逐段阅读并映射主张 | 0 |
| 博客图示 | 8 幅：架构、presentation streaming、eager dispatch、感知时延、两幅 cache、memory extractor、snapshot eval | 静态图原图检查；GIF 分帧检查 | 0 |
| 仓库文本 | 562 个 UTF‑8 文件，88,497 行 | 源码、文档、测试和配置完整审阅；lockfile/重复数值 fixture 结构化逐项检查 | 0 |
| 仓库图片 | 9 个 WebP 商品图 | 逐图视觉检查 | 0 |
| 仓库总计 | 571/571 tracked files | 固定提交审读 | 0 |

分区复核：公共运行时/根目录/文档/插件/脚本/顶层测试 72 文件、12,928 行；shopping 与 storefront 180 文件（171 文本、20,998 行，另 9 图）；merchant、四套 API/portal 与 shared host 242 文件、36,983 行；示例数据、根配置及 web-shared 顶层 77 文件、17,588 行。完整路径见[逐文件覆盖清单](anthropic-commerce-agents-audit-manifest-2026-09-02.tsv)。

“完整阅读”在这里不是把生成的 lockfile 当散文解释：人工编写的源码、文档、测试、配置全部读完；生成依赖锁和大体量重复 fixture 采用解析、类型/引用/范围/异常文本检查；图片采用视觉检查。

### 2.2 这份报告能证明什么

- 能证明固定提交中的代码结构、显式约束、测试意图和静态缺陷。
- 不能仅凭静态阅读证明真实并发、供应商 API、支付/IAM、Claude 平台兼容性或线上指标。
- 本地没有为参考仓库安装 `pytest` 依赖，因此本次没有运行它的测试或八个 Web build；风险项均标作“静态审计发现”，不是已复现的线上漏洞。
- 博客中的单 Agent 对比、90–99% cache hit、约 100k tokens 下 1.5–2× 读取速度、异步记忆提取 13% recall 提升等数字，均是 Anthropic 的内部经验；原文没有公开样本、基线、方差或复现实验，不能直接变成 Open‑Acciowork KPI。

### 2.3 Open‑Acciowork 的比较口径

稳定结论以 `036ffc6` 为基线。审读期间工作树存在大量并发、未提交修改，涉及 `commercial_actions.py`、`presentation_tools.py`、`untrusted.py`、前端卡片以及相应测试。报告把它们单列为“开发中”，不把未提交代码写成已经交付的能力。

## 3. 博客逐项主张 → 仓库实现 → Open‑Acciowork

| 原文主张 | 参考仓库实际实现 | Open‑Acciowork 现状 | 决策 |
|---|---|---|---|
| 一个模型运行标准 agent loop；不在前面放 intent router，也不按领域拆固定 agent | shopping/merchant 各定义一套 prompt、skills、tools、gates，再落到 Messages API、Agent SDK、Managed Agents 三条路径 | `AGENTS.md` 已明确单主 Agent；普通模式只允许少量一次性只读支线，Deep Research 才有固定研究分支 | **保持现状**；不要按 Demand/Supply/Capital/Sales 拆常驻 Agent |
| 高上下文耦合场景优先 Skills，不优先 subagents | shopping 与 merchant 各 5 个 Skill；merchant 仅把分析型、上下文污染大的任务委派出去 | 已有 Skill 渐进披露和代码级子代理写入拒绝 | **已对齐**；子代理只用于窄、可压缩、只读任务。不能把“单 Agent”当普遍定律 |
| Prompt 还是 Skill 由真实使用频率决定；约 1/3 流量是初始经验线 | 高频 grounding/cart/checkout/presentation 在 prompt，长尾流程在 Skill；可预测上下文由 host 预注入 | 已有稳定安全前缀与按需 Skill，但没有流量/触发率数据证明当前摆放最优 | 增加 `skill_loaded`、prompt bytes、请求分类统计，再按真实分布迁移；安全、法律、品牌、关键用户约束永远留在系统边界 |
| 工具调用既有核心系统，不在 tool 内重写多年业务规则 | 用 `StorefrontBackend`/`MerchantBackend` 定义端口；但 runnable vertical 实际是 JSON/mock backend | supplier/customer 目前主要对调用者提供的记录做过滤、排序和计算；connector 多为目录、凭据健康和撤销 | **最大产品缺口**：先建真实 Catalog/Inventory/Pricing/Supplier/CRM/Messaging ports 与 adapters |
| Tool result 只返回模型推理需要的字段，并给可行动错误 | 公共 `ToolExecutor` 把成功、错误、blocked、UI、progress 统一成 `ToolOutcome` | MCP 工具已有结构化返回，但错误/缺失字段契约尚未用真实 adapter 验证 | 为每个 port 定义 typed success、typed unavailable、retryable error；未知 metric 必须报 unavailable，不得回退成别的数据 |
| UI component 本身是 typed tool；服务端校验、补全并发事件，客户端渲染 | `present_*` 调用只传 ID，服务端按 session provenance hydrate；支持 partial/final frame | 稳定 HEAD 仍有 raw JSON/Markdown；当前 WIP 已新增 ledger-ID-only presentation 与前端 runtime decoder | 完成并收口 WIP；先交付 supplier comparison、unit economics、decision brief、commercial action preview 四张卡 |
| 端到端时延是模型轮数、tool 时延和 token 时延总和；独立工具并行、参数闭合即 eager dispatch | Messages loop 自己实现 eager dispatcher；Agent SDK 路径依赖 SDK；Managed 路径依赖平台 | SDK-only 架构应已继承平台调度，但当前没有测量证明，也不该再写一套 loop | 不复制 `turn.py`；记录 TTFT、first useful UI、tool queue/run、turn count、total task latency |
| 感知时延靠进度文案和渐进卡片，而不牺牲模型能力 | `progress`/partial UI 事件与前端逐步渲染 | 已有 SSE/partial 消息；typed progressive UI 尚未稳定合入 | 从真实 tool args 生成短进度；partial 必须可校验、可取消、可被 final 覆盖，不能成为权威状态 |
| Prefix caching 要按 global → session → volatile 排列，并滚动断点 | Messages 路径有静态 system/tool/历史断点；Agent SDK/Managed 交给宿主 | `agent_core.py` 已按稳定到易变排序，但缺 cache hit、缓存 token、prompt byte drift 指标 | 保持顺序；加字节哈希与 cache telemetry。不要为复制 cache marker 引入手写 loop |
| 模型与 effort 通过全量 eval sweep 选择，优化 cost per completed task | 仓库给出配置面与测试框架，没有公开博客数字的复现实验 | Provider registry/effort 配置存在；没有 model × effort × query mix sweep | 在真实 Green Path 和 snapshot suite 上比较质量底线、p50/p99、cost-per-success；每个候选 prompt 可分别调优 |
| 长期记忆是 typed database record；按人和权限隔离，能看、改、删，有 TTL 和部署开关 | 有类型、blocked patterns、retention、异步 extractor；示例落 JSON 文件 | proposal → user approval → correction/rollback 的治理更适合本产品；但来源锚点、删除/导出、TTL、按请求召回仍不足 | **保留人审，不复制自动直写**；异步 distiller 只能产 proposal，且只读 user/assistant 文本 |
| 记忆读取分 always-in-context、request prefetch、lookup 三层 | Messages 路径较完整；SDK/Managed 不完全等价 | 目前主要注入最近的 approved memory，并另有搜索工具 | 改为小型固定事实 + 请求相关预取 + lookup；敏感事实默认不全量注入 |
| 模型只 stage，人或 policy 才 apply；apply 时重验当前 policy | merchant change ledger + host/platform approval；shopping checkout 没有 charge method | 稳定 HEAD 没有真实商业写入；WIP 已有独立 reviewer、审批有效期、project revision CAS、当前 policy 重验，且只生成 `NOT_EXECUTED` request | WIP 设计优于参考 demo，应该保留；接真实 adapter 时再加外部 ETag/CAS、幂等键和执行 receipt |
| 写入和展示只接受服务端签发、当前 session 已见过的 ID | per-session seen products/listings/campaigns；delegate read 不扩大可写集合 | Decision/Artifact 会拒绝未知 ledger ID，但 Evidence 的 source metadata 仍可能由模型陈述；WIP presentation 已开始 server join | 新增 `SourceObservation/ToolReceipt`：身份、tenant、session、source、retrieved_at、hash 由服务端签发；证据、卡片、动作只引用 receipt ID |
| 限额按写后最终状态检查，单 session 写串行化 | cart 数量、价格变动、折扣、restock、budget cap 都在 gate | 权限与预算骨架已有；WIP commercial store 已加进程/跨进程锁 | 所有商业动作统一检查 resulting state；补重复提交、并发调用、超时、撤销、部分失败测试 |
| 第三方内容统一 sanitize + fence + hard cap，围栏内只当数据 | 公共 sanitizer 处理控制符、bidi、伪角色/工具标记并围栏 | 稳定 prompt 有“工具结果是数据”；当前 WIP `untrusted.py` 已集中来源 profile、边界替换和字段/总量上限 | 完成所有 ingress 覆盖：web、browser、upload、channel、connector、memory、presentation；围栏只是纵深防御，不能替代 ID/provenance |
| Evals 构造状态 snapshot，评价最终状态/最后写入/渲染，不约束路径 | 有 fixture、正反例、安全/跨能力测试与 eval-authoring plugin | 当前只有 4 个合成 L2 任务；`minimum_pass` 是描述性文本，Green Path 更偏固定轨迹/事件计数 | **当前最薄弱环节**：把自然语言标准变成执行断言，覆盖脏历史、数据面注入、空/超时/并发和跨能力 |
| 多团队所有权跟随核心系统；变更带 cases；定向 PR CI、nightly 全量、canary/kill switch | repo 有 CI/plugin/check scripts，但不是完整企业发布系统 | 已有 central constants、Skill 版本与 org policy；缺按变更选 case、线上 canary/cache/cost gate | 在真实 adapter 以后落地 owner manifest、case impact map、capability kill switch 和 release freeze |

文章的完整主张见[官方原文](https://claude.com/blog/the-anatomy-of-effective-commerce-agents)。其中“更换模型只是配置 + sweep”应视作愿景，不是事实保证：模型的 prompt 服从方式、tool calling、streaming、context 和安全行为都会变化，必须重新跑完整评测。

## 4. 参考仓库到底实现了什么

### 4.1 总体结构

```text
                 一套 role definition
        prompt + skills + tool schemas + code gates
                         │
          ┌──────────────┼──────────────┐
          │              │              │
    Messages API     Agent SDK     Managed Agents
      手写 loop       SDK loop       平台 loop/MCP
          └──────────────┼──────────────┘
                         │
      StorefrontBackend / MerchantBackend ports
                         │
        retail / travel / telecom / entertainment
                 JSON 与 mock host
```

仓库 README 明确说两种 agent 的定义会运行在三种 runtime 上，也明确说明所有公司/人物/商品为虚构、不会下单或扣款、merchant write 只会 staging，真实业务规则、授权和合规属于部署方。因此它是高质量蓝图，不是 production-ready commerce backend。[固定提交 README](https://github.com/anthropics/commerce-agents/blob/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf/README.md)

### 4.2 公共 harness

- Config 把影响 prompt bytes 的字段与纯运行时字段分开，Pydantic 拒绝未知字段；能力关闭会真正移除 tool、prompt、grounding 和 executor，而不是只在 prompt 里说“不要用”。
- `ToolExecutor` 是三条消费路径的统一执行面，规范化 schema 错误、领域错误、blocked/held、UI、progress 和 tool result。
- 所有 backend read 经过固定 fence；presentation 只接 ID，再从 provenance registry 找原始记录。
- `EagerDispatcher` 在 tool JSON block 闭合后启动一次执行，让它与后续模型输出重叠。
- Prompt assembly 把静态块、per-turn context、tool breakpoint 和滚动历史 breakpoint 分开。
- Memory 有类型、写入过滤、retention、dedupe 与 purge generation；Skill body 被视为可信部署资产，外部 tool result 被视为不可信数据。

这些能力集中在 [`commerce-common`](https://github.com/anthropics/commerce-agents/tree/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf/commerce-common/commerce_common)。

### 4.3 Shopping agent

- [`StorefrontBackend`](https://github.com/anthropics/commerce-agents/blob/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf/shopping-agent/core/shopping_agent/backend.py) 抽象 catalog/cart/account/orders/policy/fulfillment；agent 接口没有 payment/charge 方法，checkout 只生成 host handoff。
- Session 保存 `seen_products`；cart write 只能使用本 session 已读对象，价格、库存、订单、履约和 disclosure 由服务端重取。
- 搜索、terms、订单等高概率上下文可 deterministic pre-ground；五个 Skills 负责 discovery、research、planning、care、memory。
- 前端按 typed UI registry 渲染 partial/final frame，四个 vertical 再扩展 itinerary、plan matrix、venue map 等领域卡片。

最可迁移的模式是 `seen_opportunities/seen_suppliers/seen_evidence → ID-only card/write → server hydrate`，而不是它的购物领域对象名。

### 4.4 Merchant agent

参考实现的理想链路是：

```text
读取 canonical record
  → session provenance
  → stage_* 生成 canonical diff
  → STAGED + server-enriched preview
  → 人在 host surface Approve
  → 一次性 approval mark
  → apply gate 重验 provenance / guardrails / approval
  → 写 live state
  → APPLIED event
```

- 写目标必须先由当前 session 读取；delegate 使用独立 scratch provenance，不能扩大主 Agent 的写权限。
- listing 内容更新要求完整记录；有 variants/options 时价格与库存动作下沉到 variant。
- Messages 路径实现最完整：forced grounding、eager dispatch、partial typed UI、follow-through reminder、host approval、memory extraction 和结构化 analysis delegate。
- Agent SDK 路径由 host 预取 grounding，turn 后汇总 UI；analysis 只回自由文本，没有 Messages 路径的 `submit_analysis`/canonical metrics 验证。
- Managed Agents 路径把 `apply_change`/`save_memory` 审批交给平台 `always_ask`，没有完整 analysis、forced grounding 和 follow-through；业务 MCP 内部 approval gate 为避免双重批准而关闭。

因此“三条 runtime 共享同一角色定义”不等于三条路径行为完全等价。Open‑Acciowork 只有 SDK runtime，反而减少了需要长期维护的语义漂移面。

### 4.5 Fixture 与共享 Web 协议

- 四个 vertical 的 `merchant_messages.json` 都故意放入数据面 prompt injection：伪造平台运营通知要求全店降价，以及伪造 `apply_change` JSON 并要求导出客户名单；retail 还包含伪造闭合 `</merchant_data>`。这些是很好的 adversarial seeds，但只有评测明确断言“没有 stage/apply/export”时才算安全证据。
- 示例数据不是随便凑的展示 JSON：catalog、inventory/occupancy/pacing、orders、policies、users、campaigns 和 metrics 之间存在可供四个 vertical 使用的领域关系。不过它们仍是可变的 demo source，不是外部权威系统。
- `web-shared` 的协议包含 `text_delta/tool_call/tool_result/ui/ui_partial/cart_update/change_update/progress/turn_complete/error`，以及 pending/partial/retrying/final UI slot；这套事件词汇值得借鉴，但解析和状态合并实现不能原样移植。

## 5. 参考仓库的静态风险与不要照抄之处

### 5.1 高优先级

1. **Merchant apply 非原子、缺外部状态 CAS。** `ChangeLedger.apply` 先把记录标成 APPLIED，vertical backend 随后才写 live state；后一步异常时记录已不可安全重试，也可能部分落地。Apply 不验证当前 live value 是否仍等于 preview 的 `before`，并发操作可能覆盖更新。
2. **Campaign 可能“APPLIED 但无业务动作”。** shared fixture 只会更新既有 campaign 的 budget；新 campaign、audience、copy 没有写入 live campaign system，四个 vertical 却都可回 APPLIED。
3. **未知 metric 被伪装为已有数据。** mock backend 对陌生 metric 回退到 sales/revenue series，同时保留调用者请求的 metric 名，容易把不存在的利润/退款率包装成 grounded 数字。
4. **Managed MCP 把平台批准当外部边界。** MCP server 默认关闭内部 host approval；若被错误暴露或 gateway 身份绑定错误，调用者可能枚举 pending change 后直接 apply。生产系统必须在业务层绑定 tenant/operator/approval token。
5. **Demo 没有认证与真实 connector。** README 已明示示例 host 无 authentication、MCP 只应 loopback；不能直接暴露。
6. **JSON memory store 不具备生产并发安全。** 无跨进程锁的 read-modify-truncate-write 可能丢更新/损坏；同步 I/O 会阻塞，purge generation 与随后异步写之间仍有 TOCTOU。
7. **SDK skill materialization 可能删除别人的 Skill。** 它会清空运行 cwd 的 `.claude/skills` 中非当前源项；只有专用临时 cwd 才安全，不能指向真实用户项目。

### 5.2 中优先级

8. 公共 fence 声称 hard cap，但先截 body、再追加 wrapper/`...[truncated]`，最终字符串可超过 `max_chars`；字典 key 清洗碰撞还会静默覆盖值。[`fencing.py`](https://github.com/anthropics/commerce-agents/blob/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf/commerce-common/commerce_common/fencing.py#L135)
9. “read-only delegation”部分依赖调用者给 delegate 的工具集与约定；数据类型本身仍可携带 backend/config/state 句柄，不能单独构成能力安全边界。
10. Presentation partial 的嵌套空列表判断和 signature 设计可能发出空 frame，或漏掉同长度列表中的内容变化。
11. Messages、SDK、Managed 的 grounding、analysis、memory 和 follow-through 不等价；platform seam 测试只证明构造器兼容，不证明线上 API/行为兼容。
12. Managed MCP 依赖 FastMCP 私有 `_tool_manager`，并假定多次调用维持同一 connection/executor/provenance；升级或连接模型变化都可能破坏约束。
13. Merchant digest 的 `headline/why_it_matters` 仍可来自模型；未知 `ref_id` 也可能继续渲染，所以 typed card 不天然等于 grounded card。
14. 四套 Web generative registry 大量使用 TypeScript cast，没有统一 runtime decoder；畸形 partial 或协议漂移可能 crash。Open‑Acciowork 当前 WIP 的 discriminated validator 更值得保留。
15. 示例里存在 stale UI：旧 hold/checkout 卡仍可能可点击，历史卡与当前状态漂移；client-derived nights/savings/occupancy 也会产生错误。所有价格、可用性、费用和动作状态必须由 server hydrate。
16. UI 还有辅助功能和状态问题：重复 DOM id、drawer focus/modal 语义、加载失败卡死、硬编码时间等；参考视觉实现不能原样当产品组件库。
17. `web-shared/api.ts` 的自制 SSE parser 只处理精确的单行 `event: `/`data: `，没有 decoder flush、EOF buffer、runtime schema、重连或 event id；畸形 frame 被静默丢弃。
18. 流断开后，只要已有 assistant segment，`turn.ts` 就可能不显示错误并把剩余 partial 强制成 final；用户会看到像成功完成的残缺结果。
19. 无 `stream_id` 的 final 依同 component 寻找 slot，重复组件可能互相覆盖；Inspector 又按“同名第 k 次”而非 tool-use id 配对 call/result，并发乱序时会串联错误证据。
20. profile 切换期间保留旧 session/API/data，存在短暂的跨 profile 陈旧数据窗口；通用金额 formatter 还会根据字段名猜测并默认 USD。两者都不适合多账户、多币种产品。
21. Memory forget API 把任意非 `null` 响应（包括 `{ok:false}`）当成功，可能让 UI 声称已删除而事实仍在。

### 5.3 运维与文档脚枪

- `screenshot_tour.py` 对任意 `--base-url` 自动点击首个 Approve；`smoke_chat.py` 对远端可创建 session、批准/应用 change、释放 hold，却没有 demo-origin 防护。
- deploy 脚本先上传 Skills 再验证环境/创建 agent，失败会遗留孤儿资产，且没有 rollback；verify 脚本在没有 npm 时跳过 build 仍可给出通过印象。
- install 脚本用 `python` 检查 venv、裸 `pip` 安装，解释器可能不一致；依赖无 hashes，部分 build dependency 未固定。
- Plugin 的 scaffold/review 文档存在“extend vs replace 默认安全词表”、review 前置修改 `CLAUDE.md` 等危险歧义。
- CI 的 dependency-confusion tripwire 依赖实时 PyPI 和 `pip` 英文错误串，既脆弱又不能消除检查与安装之间的 TOCTOU；Web job 只有 `npm ci`/build，没有独立 lint、test、typecheck 或事件协议契约测试。

## 6. Open‑Acciowork：已有、开发中、真正缺失

### 6.1 稳定 HEAD 已有

- **单一主 Agent 与代码级委派边界**：主 Agent 是唯一项目真相写入者；子代理的文件/Shell/ledger/automation 写入由 `can_use_tool` 拒绝。
- **稳定 Prompt + Skills 渐进披露**：安全前缀固定在首位，系统提示按稳定到易变排列，Skill 运行时按需加载并有版本/hash snapshot。
- **结构化项目真相**：Evidence、Decision、Artifact 及 source run/关联 ID；未知 evidence/hypothesis/artifact ID 会被拒绝。
- **权限与审计**：deterministic deny/ask、once/run/persistent grant、事件脱敏、trace/session/run 关联、SSE 恢复。
- **受控记忆**：Agent 只能 proposal，owner approval 后才注入；已有纠正、rollback、容量限制。
- **SDK-only**：使用官方 Claude Agent SDK extension points，没有第二套 model client 或手写 tool loop。

相关入口：[`AGENTS.md`](../AGENTS.md)、[`agent_core.py`](../backend/src/ophira_backend/agent_core.py)、[`sdk_adapter.py`](../backend/src/ophira_backend/sdk_adapter.py)、[`ledger_tools.py`](../backend/src/ophira_backend/ledger_tools.py)、[`memory.py`](../backend/src/ophira_backend/memory.py)。

### 6.2 当前工作树开发中，不计入 HEAD

- `untrusted.py`：按来源集中管理 field/aggregate cap、控制符/bidi/伪边界处理和固定 envelope；比参考仓库的实际 hard-cap 实现更严谨。
- `presentation_tools.py` + `PresentationCards.tsx`：只接 ledger ID，服务端 join，最多 12 项，输出再次 sanitize，前端有 discriminated runtime validation。
- `commercial_actions.py`：server-issued 256-bit action ID、独立 human reviewer、审批有效期、project revision CAS、apply 前当前 policy 重验、进程内 + `flock` 跨进程事务、幂等返回。
- 该 commercial apply 诚实地只生成 `NOT_EXECUTED` execution request；没有 adapter，不声称已产生外部副作用。

这些设计应继续，而不是为了贴近参考仓库退化成 session approval set 或“先 APPLIED 后写业务状态”。但控制面总体仍没有完整用户/tenant auth；commercial approval 的 shared gateway secret 只是局部边界，不能代替产品身份系统。

### 6.3 真正缺失

1. 至少一个真实 Provider + 真实业务 connector 的可复现 Green Path。
2. 从真实读取产生的 server-issued observation/receipt；当前 ledger 引用完整性不等于 source authenticity。
3. 真正执行外部写入的 adapter、外部版本重验、幂等、失败恢复和 execution receipt。
4. 产品级 authn/authz、tenant/user/role/account scope；不能从模型参数或未受信任 header 派生身份。
5. 可执行的 snapshot eval；当前四个合成任务和描述性 `minimum_pass` 会允许“看似跑过”的假绿。
6. 真实 cache/latency/cost telemetry、model × effort sweep 与发布门禁。
7. 用户可见的记忆来源、敏感性、删除/导出/TTL 和按请求召回。

## 7. 建议的 P0 实施顺序

### P0-1：先建立 principal 与第三方数据的统一信任边界

交付：

- 每个 API、Run、Tool、Memory、Ledger、Action 绑定 `tenant_id + principal_id + role + session_id`，身份只由认证控制面注入。
- 所有 web/browser/upload/channel/connector/memory 输入进入统一 untrusted sanitizer；记录截断和边界移除 audit。
- URL fetch 补 DNS 解析后连接地址绑定/重验，避免 DNS rebinding 的检查—使用时间差。

验收：跨用户、跨租户、伪造 header、伪角色/工具标记、bidi/control、超长 payload、redirect/SSRF/DNS rebinding 全部 fail closed；日志不泄露 secret。

> 2026-09-03 实施状态：已落地 trusted-gateway 签名 principal、短时效/nonce
> replay/method-path 绑定、项目 membership/role 重验，以及 Run、Memory、receipt、
> commercial action 和无人值守 automation 的身份继承；`local_only` 明确只允许直连
> loopback 单操作者。当前边界仍是**可信 principal + 单 deployment tenant**，
> `auth_status.multi_tenant=false`。Provider、Agent、Skill、Connector、policy 等全局 store
> 尚未物理 tenant partition，Feishu 在 trusted mode 也会在缺少 durable channel identity
> mapping 时返回 503，因此真正 production multi-tenant P0-1 仍未完成，不得对外宣称完成。

### P0-2：建立 server-issued `SourceObservation/ToolReceipt`

建议字段：`id, tenant_id, principal_id, session_id, run_id, tool, canonical_source, retrieved_at, content_hash, object_type, object_version, sanitized_payload_ref, expires_at`。

规则：

- 只有 adapter/host 能创建 receipt；模型不能提交 URL、价格、库存、来源时间来冒充权威元数据。
- Evidence、Decision、Presentation、CommercialAction 都只引用 receipt/ledger ID；服务端 hydrate 展示与写入参数。
- delegate 的 scratch reads 不自动进入主 Agent 的 writable provenance。

验收：伪造、过期、跨 session/tenant、仅由 user 文本或 data-plane injection 提供的 ID 全部拒绝；服务器已知对象能回放且 hash/version 可核验。

### P0-3：完成第一条真实 Opportunity-to-Action 垂直切片

推荐先做“供应商发现 → 报价归一 → 单位经济性 → 询盘审批发送”，因为它正好覆盖产品核心，也能验证读、算、判、展示、写五层：

```text
真实 Supplier/Marketplace/CRM read
 → receipt + evidence
 → deterministic quote / landed-cost calculation
 → supplier comparison + decision brief
 → stage outreach
 → authenticated reviewer approve
 → Messaging adapter send
 → external receipt + ledger/audit
```

验收：sandbox/test account 中完成一条真实外发；recipient、内容、credential scope、政策、预算、对象版本在执行前重验；重复 apply 只产生一次外部副作用；超时/429/5xx/部分失败可恢复且不假报成功。

### P0-4：把 WIP commercial action 完成到不可绕过

- Preview 存不可变 canonical diff、before-state version、policy version、expires_at、idempotency key。
- 审批人必须与 staging 身份不同，且来自 auth principal；权限、policy、额度、resulting state、外部 ETag 在 commit 前再次检查。
- 先成功提交外部系统，再以同一幂等事务/补偿协议记录 APPLIED；失败保持 retryable/failed，而不是提前 APPLIED。
- UI 文案、chip 正则、按钮隐藏都只做 UX，绝不作为授权证据。

验收：并发更新、stale preview、双击、跨 session、审批过期、撤销、adapter timeout、写成功但 receipt 丢失、ledger 写失败均有测试。

### P0-5：让 Green Path 与评测成为可执行契约

- 每个 case 构造完整 snapshot：system/config、messages、project ledger、memory、receipts、权限、mock/real backend state。
- 断言最终 Evidence/Decision/Artifact/Action、最后 presentation payload、外部副作用和关键安全不变量；不限制合理工具路径。
- 正例配反例；覆盖长且矛盾的 history、user injection、data-plane injection、empty、timeout、malformed partial、并发、跨能力任务和真实事故。
- 先给第一条核心 flow 建 30 个高价值 case，再增长到文章建议的 50–100；所有 `minimum_pass` 转成代码 grader 或经 gold 校准的 judge。
- PR 跑 core safety + touched flow + neighbor boundary；nightly/release 跑全量。故意植入 provenance bypass、stale apply、fake grounded metric 时 CI 必须失败。

验收：至少一个真实 Provider 和一个真实 connector 完成“读取 → 证据 → 决策 → 预览 → 批准 → 应用 → 审计”；输出 pass rate、p50/p99、cache read/write tokens、总成本和 cost-per-success。

### P0-6：完成 typed progressive UI 与可观测性

- 第一批卡片：`source/evidence comparison`、`supplier comparison`、`unit economics`、`decision brief`、`commercial action diff`。
- Tool args 只放有序 ID/布局；服务端 hydrate current state；客户端只消费版本化、runtime-validated discriminated union。
- 历史卡显示 snapshot 与 current-state badge；过期、释放、撤销或已应用动作不可点击。
- 指标：TTFT、first progress、first useful card、tool queue/run、turn count、total task time、cache hit、prompt prefix hash、tokens、cost、completion。

验收：刷新/恢复历史不需要解析模型 Markdown；畸形 partial 不 crash；final frame 原子替换 partial；旧卡不可能执行当前动作。

## 8. P1：在 P0 可信闭环上再优化

1. **Memory 三层读取**：少量 always context；按 request signal 预取；其余 lookup。异步 distiller 只读 user/assistant text，只能创建带 run/anchor 的 proposal。补 delete/export/TTL/per-deployment switch。
2. **Traffic-driven Prompt/Skill**：用真实触发率而非主观判断；安全、法律、品牌和关键身份事实不随频率移出系统边界。
3. **Model/effort sweep**：按真实 query mix 加权；为候选模型分别修一次失败 prompt 后再比较；以 completed task 计成本。
4. **发布治理**：每个 tool/skill/adapter 有 owner；变更携带 cases；canary、kill switch、peak freeze、真实 incident 回灌。
5. **更多业务流**：客户发现/CRM 跟进、Listing 发布、库存/定价、跨渠道 campaign；每条都复用同一 provenance/staging/eval 契约。

## 9. 明确不要复制

- 不复制参考仓库的 Messages API 手写 loop；Open‑Acciowork 的 SDK-only 原则能减少 session、permission、streaming 和 cache 的重复实现。
- 不把 localhost MCP、固定环境身份、shared secret 或平台 `always_ask` 当完整 IAM。
- 不把 mock JSON backend、未知 metric fallback、campaign no-op、客户端派生价格/可用性当业务事实。
- 不在没有 runtime schema、取消、幂等和 final reconciliation 时启用 token-level eager presentation streaming。
- 不用模型文案、正则或 UI chip 证明事实、偏好、来源或批准。
- 不让任意外部图片 URL、历史卡片或客户端计算值成为当前权威状态。
- 不默认预载全部 memory/context；这扩大权限泄露、错误召回和 cache 成本。
- 不使用会清理真实项目 `.claude/skills` 的同步方式。
- 不让 smoke/screenshot 脚本对任意远程 origin 自动 approve/apply。
- 不把 Anthropic 的内部数字或“模型更换只是配置”写成产品承诺。

## 10. 最终判断

Anthropic 文章最重要的不是“commerce agent 应该会搜索、比较和下单”，而是把 Agent 变成一个**受已有业务系统、身份、状态机、批准面和评测共同约束的客户端**。参考仓库很好地展示了这个方向，也诚实暴露了 demo 与生产之间的缝。

Open‑Acciowork 的单主 Agent、SDK-only、结构化账本、权限治理和记忆人审已经是正确地基。接下来不该继续堆泛化能力，而应选择一条真实商业闭环，把 `auth principal → source receipt → server-hydrated UI → stage/review/apply → external receipt → snapshot eval` 一次做透。完成这条链后，其他供应、客户、定价和营销能力才会变成可复制的业务模块，而不是更多“Agent 看起来会做”的演示。
