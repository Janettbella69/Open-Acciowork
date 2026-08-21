# 《生产级 AI Coding 实战教材》（上下册）× OPHIRA 对照审计

- 日期：2026-08-21 · 审计人：Claude（负责人待复核）
- 教材来源：飞书 wiki——上册 `SWxUwJEo1iPHrVkPXNEckOYRnZc`（立项/架构判断/解耦/推进与卡壳）、下册 `Xk9FwBrgMi4WIuklvasc0c4TnAb`（前端/部署/质量上线/记忆协同/失败档案/四象限）、配套《AI Coding 的七项底层能力》`Q6H6w7nlqiIcKxktxwkcQpb6n4b`；三篇全文通读。
- 方法：逐套路对照本仓现状，证据只引真实文件/提交。
- 状态口径：✅ 已做且有证据 · 🟡 部分/习惯有但未制度化 · ❌ 没做 · ➖ 不适用/未到阶段

## 第一章 立项

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 1.1 物理隔离 | ✅ | 项目唯一目录 ~/Open-Acciowork（用户明确指令）；运行期每项目独立 workspace + `can_use_tool` 路径包含检查（sdk_adapter.py） |
| 1.2 先读懂再动手 | ✅ | AGENTS.md「Before changing code」第 1 条：先读规则/README/源码/测试/**git 历史** |
| 1.3 竞品逆向 | 🟡 | PRD v2.1 有 Accio/OpenClaw/Cowork 对标分析；但竞品是 SaaS，二进制逆向不适用。差距：未做过「深度体验 Accio + 抓交互流/网络请求」级别的拆解 |
| 1.4 四路搜索 | 🟡 | 实践中做过（Agent SDK 文档、Tenstorrent 调研等），但**未写成规则**：英文搜索、官方/论文/社区/口碑四路、要求 AI 标注信源可信度等级 |
| 1.5 权威参照系 | ✅ | 刚落地（f7ca9fb）：CLAUDE.md「参考优先级 + AI 知识盲区」+ `docs/claude-agent-sdk/` 23 篇官方快照 + 钉版安装源码为最终裁决 |
| 1.6 让 AI 先质疑再开工 | 🟡 | AGENTS.md 第 4 条要求高风险改动前澄清歧义；但没有「新特性开工前 AI 先出质疑清单」的固定仪式（见差距 G1 自检题） |
| 1.7 移植工作方式 | ✅ | 用户全局 CLAUDE.md + 本仓 CLAUDE.md/AGENTS.md 承载协作协议；跨项目复用是用户既有习惯 |
| 1.8 立项清单 | ➖ | 已过立项期；Phase 0 收口计划（docs/superpowers/plans/）承担了同等职能 |

## 第二章 架构判断

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 2.1 改错的代价判据（红绿区） | 🟡 | 实践一致（方向级决策等拍板、PRD 红线区）；但「几分钟/几小时/几周/重写」判断表未写进 AGENTS.md |
| 2.2 架构文档 + 自检题 | 🟡 | PRD v2.1 = 单一事实源，有施工顺序（Phase 0-4）和验收门槛（§6.6）；**缺自检题一节**——新 AI 会话开工前无法被验证「真读懂了」（差距 G1） |
| 2.3 显式解除兼容性顾虑 | 🟡 | 实际在做（repo 更名、Phase 0 期间多次推倒），但「研发期允许 breaking change、不留技术债」未成文；红线（§1.1）是唯一不可推翻区，边界清晰但只写了后半句（差距 G2） |
| 2.4 AI 可理解性作为架构目标 | ✅ | AGENTS.md Observability 三层日志（manifest / summary / detailed JSONL）与教材三层结构逐字同构；`digest_run.py` 专为 AI 读 trace 设计；CLAUDE.md「架构大图」为新会话导航 |
| 2.5 同时问收益和代价 | 🟡 | 用户提问习惯已有（memory: direction-decisions）；属人侧技能，不强求成文 |
| 2.6 过度设计 / 规模审计 | 🟡 | AGENTS.md「新抽象须有具体代价理由 + 加基础设施前查重」；**规模审计 6 问未成文**——7 个 MCP server、evals 工具链在增长，值得挂一道尺子（差距 G3） |
| 2.7 克制 / 可逆下线 | ✅ | 教科书级现成案例：`disallowed_tools=["Agent"]` 入口级屏蔽 + 测试断言 + 「放宽提案待用户拍板，勿自行放开」铁律（CLAUDE.md 红线）；FM-01 升级方案也走「候选待拍板」而非自动应用 |

## 第三章 解耦

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 3.2 别重复造轮子 | ✅ | AGENTS.md：禁止重建 SDK 已有的 session/subagent/permission/hook/tool 机制；红线禁手写工具循环、禁 LangChain 系 |
| 3.3 评测与产品分开 | ✅(3/3 层) | ①代码隔离：evals/tools 独立 + wheel 只打包 `src/ophira_backend` + CI 不装 backend 的独立 job（f72e73e）②模式一致：L2 经真实 HTTP API + 真实 SDK 走产品全路径，唯一分叉 = `PermissionPolicy` 自动批准（自动化必需，且打断次数仍被计量，分叉已知已量化）③用例隔离：每任务独立 project/workspace/worker env。**观察项 O1**：L2 运行写入与日常开发共用的 `backend/data`，评测产物与开发数据同池（gitignored，无泄露风险，但违背"用例环境可抛弃"的洁癖） |
| 3.4 多 AI 重复建设 / 单一事实源 | ✅ | AGENTS.md「one source of truth：事件类型/状态/工具名/错误码，消费方 import 不得重打协议串」；constants.py 落实；CI 静态门禁 |
| 3.5 框架外加一层 | ✅（变体） | 教材造翻译 Proxy；OPHIRA 反向解法同目标：只走各厂商官方 Anthropic 兼容端点（红线禁协议转换），SDK 同样一行不改。Provider Profile + `build_worker_env` = 「唯一知道有多家模型的地方」 |
| 3.6 问能力不问身份 | ✅ | FR-23 正是 capability 模式：profile 记 `native_web_search` 能力位，缺失时挂 ophira_web 降级 MCP——业务问「能不能检索」，不问「你是 Kimi 吗」 |
| 3.7 状态从会话解耦（文件账本） | ✅✅ | 与教材同名同构：FileLedger + AGENTS.md「Chat context and model memory are not authoritative storage」。OPHIRA 的立身之本 |
| 3.8 服务从产品拆出 | ➖ | Phase 4（云 Runner/session_store 外置）才到 |
| 3.9 解耦有代价 | ✅ | evals/tools 拆分的接缝已成文：sample_gold.py docstring 声明唯一接触点、tests/conftest 自带路径、README 边界说明——教材说的「藏起来的路径假设」全部显式化 |

## 第四章 推进与卡壳

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 4.1 分步汇报 / 卡住不可逆 | ✅ | 被**产品化**了：授权卡片 + §4.10 包络原则 + 外发消息高风险授权 + Bash 授权永不按 run 缓存；流程侧 AGENTS.md「minimum contact surface」 |
| 4.2 主动砍范围 | ✅ | Phase 0 收口的范围裁剪、评测「少而精」策略；用户既有习惯 |
| 4.3 换层次追问（改 3 次停） | ✅ | AGENTS.md:38 逐字在位：「If three attempts fail for the same issue, stop patching and reassess the architecture or assumption」 |
| 4.4 三段式根因 | ✅ | AGENTS.md:35 逐字在位：「investigate without edits → report evidence/options/impact → implement after confirmation」 |
| 4.5 先加日志再改代码 | ✅ | AGENTS.md:37「Add observability before speculative fixes」+ trace_id 全链路贯通 + 三层日志 |
| 4.6 用户投诉→线上根因 | 🟡/➖ | 未上线，无真实用户；链路前提已具备大半（trace_id 贯通、结构化日志、脱敏落盘）。**差距 G4**：上线 gate 前需补「用户↔run 关联、投诉可回溯」清单（服务端架构天然占优，日志本就在服务侧） |
| 4.7 需求淹没 / 需求台账 | ✅ | AGENTS.md:18 逐字在位：「Keep an explicit requirement inventory and mark every item delivered, deferred, or omitted with a reason」 |

## 第五章 前端（下册）

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 5.1 给参照物不给形容词 | 🟡 | 前端按 PRD 规格实现（四页路由/组件契约），无对标产品参照流程；未来 UI 打磨时按模板补参照物即可，属提示词习惯 |
| 5.2 截图驱动 | ✅ | 用户既有习惯（视觉产物主动 open 给用户确认） |
| 5.3 AI 用浏览器端到端自验证 | 🟡❗ | 前端只有 vitest 组件测试（27/27）；**无浏览器级 E2E**。教材强推「必须做端到端浏览器验证，优先内置浏览器工具/Claude in Chrome」。见差距 G6 |

## 第六章 后端与部署（下册）

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 6.1 先问业界模式再实现 | 🟡 | 实践中做（PRD 对标分析、SDK 文档快照）；提问习惯属人侧 |
| 6.2 多 AI 互相干扰 | ✅ | AGENTS.md Verification 逐字在位：「Stage only files owned by the current task; never sweep in another agent's unfinished changes」 |
| 6.4 危险操作物理上做不到 | ✅ | 产品化程度超过教材：sandbox 强制开启不得关闭（红线）+ worker env 先清空全部凭据再注入唯一 Provider 凭据 + PreToolUse 敏感路径黑名单 + secrets 0600/事件边界 redact() + CI 静态门禁；「查密钥不打印」= FileSecretVault 不落 profile、测试禁读 secrets/ |
| 发版唯一入口/部署 | ➖ | 未到发版阶段；届时按教材建唯一入口+guard（挂上线 gate，并入 G5） |

## 第七章 质量与上线（下册）

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 7.1 夜间批量自动化测试 | 🟡 | L2 批跑已自动化（run_l2.py 后台整轮），但每任务 n=1、无「预授权障碍处理 + 无人值守长跑 + 失败分类汇总」的夜测形态；基线报告已自记「回归口径建议每任务 ≥3 次」。见差距 G7 |
| 7.2 双 AI 协作测试（Dev/QA 分离） | 🟡 | L2 用机器断言（非写码 AI 自评）✅；错误分析开码含「Agent efficiency observations」性质的观察（FM-01/06 即是）✅；但无独立第二 AI 做 QA/交叉。并入 G9 |
| 7.3 验收看事实源不看汇报 | ✅ | AGENTS.md/CLAUDE.md「声称完成前先实际运行验证给证据」；L2 判定全部从账本事件流（verify_events）取证，不采信模型自述；greenpath 断言 run.result 与事件序列一致 |
| 7.4 上线前遥测体系 | 🟡/➖ | 服务端架构天然全量落盘（trace_id 贯通 + 三层日志 + redact），「用户不投诉也能发现」的基础好于教材案例；缺用户↔run 关联与告警面，已在 G5（上线 gate） |
| 7.5 技术诚信（对外话术=代码事实） | ✅/🟡 | 工程侧强：合约报告如实标注未测项、ophira_web 未配 key 返回结构化错误绝不伪造、评测报告标注 n=1 口径。**观察项 O2**：frontend `/` 营销页存在，未做过教材 A14 式「逐句话术×代码事实」核对——上线前必做 |

## 第八章 记忆与协同（下册）

| 套路 | 状态 | OPHIRA 证据 / 差距 |
|---|---|---|
| 8.1 会话交接（handoff） | ✅ | docs/superpowers/plans/ 收口文档 + 跨会话 auto-memory；handoff（项目级进展）与 memory（长期经验）分层与教材口径一致 |
| 8.2 为 AI 失忆设计架构 | ✅✅ | 与 3.7 同体：FileLedger 文件账本=「会话可随时丢弃」；且 OPHIRA 把 memory 治理产品化（Agent 只能提案、用户批准后生效、MEMORY.md marker 镜像）——超出教材形态 |
| 8.3 memory 组织（规则+Why+How、过期治理、代码为准） | ✅ | 开发侧 auto-memory 即此格式（type: feedback 含 Why/How to apply）；「memory 与代码冲突以代码为准、引用前先验证」是 memory 系统内置纪律 |
| 8.4 多模型交叉评审（adopt/revise/reject） | ❌ | OPHIRA 开发全程未用第二模型交叉评审。教材：重大节点（尤其上线前 review）用「问异同不问对错 + 独立思考 + 逐条 adopt/revise/reject」。见差距 G9 |

## 第九/十章（下册）

失败档案与四象限总表为教学载体，判据（改错代价 × 有无用户）已并入 2.1/G 系列；「上线是单向门」清单并入 G5。

## 配套篇《AI Coding 的七项底层能力》

该篇是**人侧能力**文档（索取未知/识别胡说/中断/提问/反身性/架构思维/借鉴），不构成仓库审计对象。三点交叉记录：
1. 「人永远不跑脚本，全闭环交给 AI」——本仓实践一致（含真端点评测、后台批跑）；唯一例外是制度性的：gold 标注必须负责人本人做（那是判断不是脚本，PRD §3.11 红线，与教材不冲突）。
2. 「解除附和压力」「你不用附和我」「包括但不限于我说的方向」——可直接并入 G4 的提问模板。
3. 教材 1.5 权威参照系 / 7.2 AI 知识盲区的两段原文，正是本仓 CLAUDE.md 2026-08-21 已落的那两节（f7ca9fb）——出处闭环。

## 结论

**总体：上册 24 条 + 下册 16 条 = 40 条套路，✅ 22 · 🟡 13 · ➖ 4 · ❌ 1。** OPHIRA 的工程宪法（AGENTS.md）与教材高度同源，多条逐字重合（4.3/4.4/4.5/4.7/3.4/3.7）；且有两处教材套路被 OPHIRA 产品化（4.1→授权体系、2.7→工具可逆下线+铁律）。真正的差距集中在「习惯已有但未成文」的四件事：

### 差距清单（待拍板，均为文档级改动）

| # | 补什么 | 改哪里 | 工作量 | 我的优先级 |
|---|---|---|---|---|
| G1 | **自检题机制**：PRD 加「§开工自检题」（5 题含"哪里最模糊你最可能理解错"），新 AI 会话/新 Phase 开工先答题 | PRD v2.1 附录 + CLAUDE.md 一句引用 | ~30 分钟 | **高**——多会话开发的理解偏差是本项目最真实的风险（教材 3.4 的病，2.2 的药） |
| G2 | **研发期 breaking-change 授权**：明文「上线前为架构整洁可推翻旧代码、不留技术债；红线区除外」 | AGENTS.md Architecture 节 + CLAUDE.md | ~10 分钟 | **高**——AI 默认保守，本项目还长期处于研发期 |
| G3 | **规模审计 6 问**：任何新基础设施/新抽象提案前先答（消费方数/团队/数据量级/并发/跨语言/外部集成） | AGENTS.md Architecture 节 | ~10 分钟 | 中 |
| G4 | **四路搜索模板**：英文搜索、四类信源、标注可信度等级 | CLAUDE.md「参考优先级」下补一小节 | ~10 分钟 | 中 |
| G6 | **前端浏览器 E2E 验证工作流**（教材 5.3）：改完前端后用浏览器工具真实走一遍 Workspace/证据抽屉/设置页，不接受"组件测试过=能用" | 工作流约定进 AGENTS.md/CLAUDE.md + 实际跑一轮 | ~1 小时/轮 | **高**——前端 27/27 组件测试从未等价于端到端可用 |
| G7 | **L2 回归批跑升级**（教材 7.1）：每任务 n≥3 取均值 + 失败按原因分类汇总 + 预授权障碍处理（无人值守长跑形态）；花真钱（约 3×2.6 USD 牌价/轮），按需触发不进 CI | run_l2.py 加 --repeat + 汇总口径 | ~1 小时开发 + 跑批成本 | 中——基线 n=1 的口径本来就自记了这个欠账 |
| G9 | **多模型交叉评审**（教材 8.4，唯一 ❌）：重大节点（上线前 review、架构变更）引入第二模型，问异同不问对错、逐条 adopt/revise/reject；日常开发不用 | 约定进 AGENTS.md + 上线 gate 清单 | 约定 10 分钟；每次评审另计 | 中——真正的触发点是上线前 |
| O1 | 评测运行数据与开发数据同池（backend/data）——先记录为已知观察项，Phase 2 再议独立 eval data dir | 本文档记录即可 | 0 | 低（按 3.9：现在拆是净亏损） |
| O2 | 营销页话术未做「逐句 × 代码事实」核对（教材 7.5/A14）——上线前必做，并入 G5 清单 | 上线 gate | 届时 | 低（未到阶段，先记录） |
| G5 | 上线前监测清单（用户↔run 关联、投诉回溯路径 + 发版唯一入口/guard + O2 话术核对 + 四象限红区清单）——挂到上线 gate，现在不做 | 上线 Phase 的 plan 文档 | 届时 | 低（未到阶段） |

> 按教材 2.1 的口径：G1–G4 是绿区文档改动（约 1 小时全做完）；G6 是工作流约定+一轮实测；G7 花真钱按需触发；G9 到上线前才真正启用；O1/O2/G5 是刻意不做并写下触发条件——这本身就是教材 3.9/2.6 的用法。全部待负责人拍板。
