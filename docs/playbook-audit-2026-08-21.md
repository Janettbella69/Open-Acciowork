# 《生产级 AI Coding 实战教材·上册》× OPHIRA 对照审计

- 日期：2026-08-21 · 审计人：Claude（负责人待复核）
- 教材来源：飞书 wiki `SWxUwJEo1iPHrVkPXNEckOYRnZc`（上册：立项/架构判断/解耦/推进与卡壳，65,784 字全文通读）
- 方法：逐套路对照本仓现状，证据只引真实文件/提交；下册（前端/部署/质量/记忆/失败档案）只有预告，未审计。
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

## 结论

**总体：24 条套路，✅ 14 · 🟡 7 · ➖ 3 · ❌ 0。** OPHIRA 的工程宪法（AGENTS.md）与教材高度同源，多条逐字重合（4.3/4.4/4.5/4.7/3.4/3.7）；且有两处教材套路被 OPHIRA 产品化（4.1→授权体系、2.7→工具可逆下线+铁律）。真正的差距集中在「习惯已有但未成文」的四件事：

### 差距清单（待拍板，均为文档级改动）

| # | 补什么 | 改哪里 | 工作量 | 我的优先级 |
|---|---|---|---|---|
| G1 | **自检题机制**：PRD 加「§开工自检题」（5 题含"哪里最模糊你最可能理解错"），新 AI 会话/新 Phase 开工先答题 | PRD v2.1 附录 + CLAUDE.md 一句引用 | ~30 分钟 | **高**——多会话开发的理解偏差是本项目最真实的风险（教材 3.4 的病，2.2 的药） |
| G2 | **研发期 breaking-change 授权**：明文「上线前为架构整洁可推翻旧代码、不留技术债；红线区除外」 | AGENTS.md Architecture 节 + CLAUDE.md | ~10 分钟 | **高**——AI 默认保守，本项目还长期处于研发期 |
| G3 | **规模审计 6 问**：任何新基础设施/新抽象提案前先答（消费方数/团队/数据量级/并发/跨语言/外部集成） | AGENTS.md Architecture 节 | ~10 分钟 | 中 |
| G4 | **四路搜索模板**：英文搜索、四类信源、标注可信度等级 | CLAUDE.md「参考优先级」下补一小节 | ~10 分钟 | 中 |
| O1 | 评测运行数据与开发数据同池（backend/data）——先记录为已知观察项，Phase 2 再议独立 eval data dir | 本文档记录即可 | 0 | 低（按 3.9：现在拆是净亏损） |
| G5 | 上线前监测清单（用户↔run 关联、投诉回溯路径）——挂到上线 gate，现在不做 | 上线 Phase 的 plan 文档 | 届时 | 低（未到阶段） |

> 按教材 2.1 的口径：G1–G4 都是绿区改动（改错代价=几分钟），拍板后可一次做完；O1/G5 是刻意不做并写下触发条件——这本身就是教材 3.9/2.6 的用法。
