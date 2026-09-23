# PRD 全量交付清单

基准：`OPHIRA-PRD-and-Technical-Architecture-v2.md` v2.2，2026-09-05。
本清单跟踪“完成 PRD 全部功能”的持续任务，不替换 PRD、不降低验收门槛。
基线提交：`0dfff72`。状态为 **delivered**（指定范围已有证据）、
**deferred**（仍需交付，原因列明）或 **omitted**（PRD 明确不做）。
“本地实现”不表示真实服务、长期运行或生产部署验收通过。

## 功能要求

| 要求 | 已交付边界 | 尚需交付 / deferred 原因 |
|---|---|---|
| FR-01 单主 Agent | delivered：SDK 主循环、普通只读支线限制 | 保持回归；不引入固定角色团队 |
| FR-02 唯一 SDK runtime | delivered：架构静态门禁、DeepSeek 三轮真端点链路 | 每次 SDK 升级重新认证 |
| FR-03 原生工具 | delivered：Read/Web/Skill/MCP/Bash/Write/Edit 实测 | 跨 Provider 能力归 FR-05/06 |
| FR-04 自由规划 | delivered：无固定业务工作流 | 开放任务质量继续由 L2/L3 验收 |
| FR-05 Provider | delivered：Registry/Vault/快照/UI | deferred：其他上架组合的真实合约、条款与区域复核；需要合法账号 |
| FR-06 预检 | delivered：SDK 预检、模型身份与错误分类 | deferred：其他 Provider 合约；DeepSeek vision/无效凭据快速失败仍明确降级 |
| FR-07 Run/会话 | delivered：SSE、取消、恢复、重试、重启孤儿修复、并发事件序号与汇总恢复 | deferred：其余恢复故障矩阵、性能基线 |
| FR-08 隔离 | delivered：项目路径、符号链接、独立 worker 配置；原生进程时长/输出监督、Linux systemd CPU/内存/任务数门禁及配置/已验证状态区分（Linux验收见下方记录） | deferred：真实模型与原生沙箱的Linux联合验收、API宿主MCP资源限制、生产负载与租户物理隔离演练 |
| FR-09 来源账本 | delivered：同 Run/session 的服务器 source receipt | 保持来源、失效、回放和内容完整性回归 |
| FR-10 决策 | delivered：证据关联与失效联动 | 开放任务质量另行验收 |
| FR-11 领域 MCP | delivered：确定性商业计算、用户记录加工、显式结算计划的现金流/日末与保守日内资金缺口；TikTok Shop/Shopify/SHEIN/Shopee项目指定店铺的主Agent只读MCP与来源凭证；Shopify近期订单、SHEIN SPU/SKC分层商品与分页/上架状态、Shopee商品与SKU价格库存/分页 | deferred：真实供应商/客户/市场/法规/物流/汇率授权数据适配及已实现商家接口的真实验收 |
| FR-12 Skill 生命周期 | delivered：发现/启停/扫描/不可变版本/配额/租约/回滚；13个内置Skill的证据参考与输出模板、固定包原生只读访问；Agent候选冻结、人审发布默认关闭、凭证恢复与独立审阅UI；独立Skill备份及含项目账本/候选库的应用冷备份、新进程恢复 | deferred：真实模型提案质量、真端点回滚重跑、跨宿主/生产恢复及其余子系统的恢复认证 |
| FR-13 权限 | delivered：范围/有效期/审计、删除确认、沙箱拒绝逃逸；可信设备单次审批；原生SDK持久等待、原Run恢复、跨进程决议竞争、消费前撤销复验；工作台内切换组织授权模式，仅 SDK 原生 default/auto 两档 | deferred：真实远程审批与生产故障矩阵；原生多工具批次须拆为单次审批 |
| FR-14 记忆 | delivered：提案人审、TTL/过期/检索/导出/删除恢复 | deferred：真实蒸馏质量、生产备份删除边界与恢复演练 |
| FR-15 自动化 | delivered：计划/心跳/收件箱本地链路；跨进程自动调度租约和崩溃幂等；手动/自动触发共用租约与持久Run重叠检查、页面并发操作治理 | deferred：真实长期运行与渠道投递 |
| FR-16 产物 | delivered：不可变文件版本/准确历史下载、同内容回退的新版本、并发登记去重；文本预览/修改对比/冲突保护/幂等保存/人类编辑溯源、编辑中断恢复与冷备恢复；Office 文件结构 | deferred：真实模型连续编辑与浏览器目视、Office 渲染 QA、布局回归；旧记录已丢失的原始字节无法补造 |
| FR-17 可观测性 | delivered：trace、三层日志、脱敏、类型化卡片；项目范围Provider/模型/Skill/MCP/错误分组指标与工作区面板 | deferred：细分事件与生产指标/性能验收 |
| FR-18 恢复 | delivered：保存产物、错误分类、重试与重启恢复；Run 起点的完整可支持工作区快照、Bash 修改恢复、人工变更预览/确认/结果重试、中断目录切换修复、真实进程退出与冷备往返；同项目执行与产物收尾租约 | deferred：真实 SDK 故障注入、生产/跨宿主恢复、容量保留管理、性能与浏览器目视验收 |
| FR-19 Deep Research | delivered：输入框内显式开关发起（默认关闭、运行中锁定）；三种原生只读研究单元的一次新建/同 Run 续接约束；共享预算/取消/原始截止时间；运行中资源 UI、来源包与主 Agent 去重落账；真实 DeepSeek 合成来源上的三单元并行/来源复核/证据合并与报告验收 | deferred：开放互联网研究质量、真实取消/耗尽/长时墙钟故障矩阵、其他 Provider 与浏览器目视验收 |
| FR-20 GUI | delivered：L1/L2 真实浏览器 smoke；项目独立店铺 profile、用户登录确认、当前页观察引用、主Agent页面动作/报表下载与来源/产物落账、真实Chromium离线链路 | deferred：真实卖家后台/验证码/风控兼容性和模型联调；真实攻击/恢复矩阵；L3 桌面桥与 OS 权限治理未实现 |
| FR-21 Connector | delivered：Catalog/Vault/health/revoke、HTTPS JSON send_message；六平台官方文档入口，TikTok Shop授权店铺/商品分页、Shopify自有店铺/token/商品/近期订单、SHEIN商家密钥/店铺/SPU与SKC商品读取，Shopee单店凭据/持久一次性刷新/商品与SKU价格库存/分页；项目店铺授权/到期/撤销、主Agent来源凭证与中英文管理员UI；项目店铺身份可关联平台Connector、独立浏览器授权及数据展示 | deferred：Temu/AliExpress及其余邮箱/店铺/CRM/社媒adapter；Shopee初次OAuth/授权码交换与订单、TikTok Shop OAuth/refresh、SHEIN授权交换/解密与SKU库存价格、平台写操作；真实授权账号/模型调用验收与外发全链；API与浏览器数据自动合并 |
| FR-22 组织治理 | delivered：单部署租户 principal/membership/策略；共享项目成员管理UI与版本冲突保护；持久团队讨论/回复/对象关联/编辑历史/解决状态；可复用项目模板、固定参考版本与来源、核对后创建和中断恢复；组织私有Skill导入人审、固定版本查看/导出、独立启停/恢复、跨进程凭证与冷备恢复 | deferred：真实身份网关联调、租户分区与隔离 runner、长期容量与浏览器目视验收 |
| FR-23 Web 兜底 | delivered：Serper/Tavily 适配与 SSRF 防护 | deferred：真实配置搜索服务和 Provider 降级 E2E |
| FR-24 IM | delivered：飞书签名入站/身份绑定/同 Agent/持久回复重试 | deferred：真实飞书/Lark 租户 E2E；微信适配未实现 |
| FR-25 移动 | delivered：项目范围配对、双端验证码批准、私有 cookie 与设备撤销/过期；单次持久审批、可选任务发起；Web Push订阅/加密VAPID/授权复验/持久重试与状态UI | deferred：native push、真实手机 HTTPS/外部Push/长期运行验收；OS 防休眠未实现 |
| FR-26 主动服务 | delivered：授权 standing intent、只读 watcher、预算与持久 claim | deferred：真实数据/Provider 长时判断质量、有效变化触达 |
| FR-27 媒体 | delivered：安全二进制接入与 Artifact/Evidence | deferred：真实图像/视频厂商适配、授权配置及验收 |
| FR-28 澄清表单 | delivered：结构化问题与同任务约束回传 | 保持 UI/API 回归 |

## FR 表之外仍属于全量范围的要求

| PRD 落点 | 状态及剩余工作 |
|---|---|
| §3.5 A–G / §6.5 | delivered：文本产物原位编辑、不可变历史、修改后知识库检索的本地闭环；deferred：需求/供给/客户多起点真实基准、真实模型连续编辑/对话与产物质量验收 |
| §3.7 / §4.7 | delivered：原生文件工具 + 显式Artifact提交候选，独立pending冻结包、人审revision/hash/当前目标CAS、静态扫描强制检查、不可变发布默认关闭和独立审阅UI；deferred：真实Provider自主提案质量、人工使用效果与生产隔离验收 |
| §3.7 P2 知识库 | delivered：项目登记文本/OCR伴随文件及有界DOCX/XLSX/PPTX原件文本索引、来源段落/单元格/幻灯片/行列/版本、权限复验API、只读MCP与中英文工作区检索、校验和一致的下载；deferred：旧版/加密/Strict Office及图表图片语义、真实检索质量/容量与浏览器目视验收 |
| §3.7 P2 团队 | delivered：共享项目成员角色管理、关联项目/Run/消息/证据/决策/文件版本的团队讨论、项目模板保存/版本/参考文件下载/新建/归档/中断恢复、组织私有Skill审核发布与成员只读版本库及中英文UI；deferred：真实组织与浏览器目视验收、长期容量及生产隔离 |
| §4.16 任务经验 | delivered：专属工具与版本/来源账本、创建时配置指纹、低信任检索注入、容量/事务恢复与人工停用UI；deferred：真实Provider自主调用及长期判断质量 |
| §4.12 事件 | delivered：有真实Run归属的evidence.added/decision.recorded/artifact.registered/artifact.updated/memory.proposed、持久幂等补发与完整SSE尾部消费；任务记忆审阅面板与按Run授权查询；deferred：浏览器目视验收、无Run项目变更和 schedule/preflight 细分事件 |
| §3.9 / §4.10 | delivered：自动调度跨进程抢占与事件汇总恢复；Bash 工作区快照与人审恢复、跨进程目录切换修复；Run固定时限与跨段输出额度、崩溃后未核对额度禁止续接、Linux worker资源限制与普通/研究运行资源UI；deferred：Linux真实模型/沙箱联合验收、其余事件故障矩阵、生产恢复/容量与性能 |
| §3.9 数据主权 | delivered：操作者CLI完整数据目录冷备份与新目录恢复，覆盖项目账本/提案/Registry/工作区检查点一致性；独立本机中英文维护UI、完整副本核对/确认发布/丢弃及进程中断重试；项目工作文件恢复UI；Provider/Connector/飞书与Push服务凭据的人审版本绑定加密导出、离线解密、确认删除与持久环境回退禁用，服务凭据显式重配、缓存撤销与后台恢复；部署根签名凭据的独立本机加密导出/确认删除/重配/恢复、停机独占锁、租户与版本核对、仅Vault启动和每次鉴权实时解析；项目业务数据与应用可见会话的人审授权ZIP导出、文件版本/记忆历史及一致性核验；文件删除事务/中断恢复与应用数据访问互斥；保留保存成果的会话删除、双向续接/原生SDK与可归属上下文清理、人审指纹确认API/中英文UI、内容为空的来源与冷备兼容；完整项目删除、工作区/版本/自动化/店铺登录与项目授权清理、模板独立副本/冷备兼容、名称确认与导航/重试防重建；deferred：删除/恢复的浏览器与生产验收、原生SDK完整会话可迁移性、部署自动切换及生产灾备 |
| §3.9 性能 | delivered：本地ASGI单/4并发各100样本，创建/首事件持久化/离线task取消P95基线；deferred：真实SDK进程、网络/SSE运输、多worker与生产负载验收 |
| §3.9 / §3.10 | delivered：项目范围按Provider/model/Skill/tool/错误的有界指标汇总；模型选择与设置中的端点/管理员填写区域、跨境及工具数据流提示，未知与Auto路由单独说明；deferred：业务成功指标、真实区域/条款认证与驻留强制、浏览器目视验收 |
| §3.11 / §6.6 | deferred：当前 L2 基线、人亲自标注的 gold、judge 校准与 TPR/TNR 门槛；不能由 AI 伪造标注 |
| 教材审计 / CLAUDE.md | delivered：有界 PDF/图片 Markdown 伴随文件、正确可选本地 PaddleOCRVL 适配、pypdf/Tesseract 明确降级、缺页/失败/截断提示与上传原件 Read 门禁；deferred：真实 VL 识别与版式质量、TextIn 坐标、生产解码容器和 OS 资源隔离；当前未安装 VL 模型或 Tesseract |
| P2-feedback-loop-spec.md | delivered：消息/证据精确锚点、原始 why、失败/取消幂等自动采集和启动补采、人审 revision/history、Skill 版本/L2 人工确认记录、显式无标签候选导出与抽样；deferred：本人 gold/FM 定版、真实 Skill 改善与 L2 验收、生产长期回流、负责人尚未定值的积压通知 |
| §4.13 / §6.4 | deferred：生产多租户隔离、跨宿主 SessionStore、密钥/备份恢复及攻击面验收 |
| §6.2 / §6.3 | delivered：已归档 DeepSeek 三连过及首批合约；其他组合不可借此宣称认证 |

## 明确不做（omitted，来自 PRD 非目标/红线）

- 固定四角色工作流、平行负责人团队、替换 Claude Agent SDK 或手写模型循环。
- 未授权自动下单/付款/批量询盘/店铺修改、把代理信号宣传为官方销量。
- 未经人审写全局记忆或自动发布 Skill；由 AI 代替负责人标注 gold。
- 共享可写宿主 Bash 作为多租户执行方案；静默跨 Provider 降级。

## 验证基线

`0dfff72`：后端 585 passed / 8 live-only skipped；前端 97 passed；
eval tools 48 passed；Snapshot 61/61；lint、TypeScript 与 build 通过。
历史真端点报告见 PRD §5/6 和 `evals/reports/`。本轮尚未新增真实外发或付费验证。

后续每项交付追加提交号、测试命令/结果、真实验收边界；所有 deferred 都保留在持续目标中。

## 2026-09-05：自动调度与账本恢复

新增交付：每计划非阻塞进程租约、按计划/到期时间稳定生成的触发标识、
Run 先持久化 claim 再启动、已有 Run 的审计/计划收尾恢复、条件推进不覆盖用户改期/暂停。
共享账本记录采用跨进程锁、私有唯一临时文件和 fsync；事件汇总记录 JSONL 字节检查点，
缺失/落后汇总从 Run 与完整事件重建，不把损坏或不完整日志当成成功恢复。

实测：6 个最初失败的调度回归用例全部转绿；真实 OS 子进程在审计写入前/后被终止的
4 个场景均只保留一个 Run；两进程 80 条并发事件序号连续；补测 Run/事件已落盘、
汇总未写入的恢复窗口。模型 runtime 使用离线替身，没有真实外发或付费调用。

验证命令与结果：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：599 passed，8 live-only skipped。
- `.venv/bin/ruff check backend/src backend/tests evals/tools`：通过。
- `.venv/bin/python -m pytest evals/tools/tests -q`：48 passed。
- `PYTHONPATH=evals/tools .venv/bin/python evals/tools/run_snapshot_evals.py`：61/61。
- 前端 `npm run test:run`：97 passed；`npm run lint`、`npm run build`：通过，既有 bundle-size warning 保留。

边界：这不是多租户认证，也不证明外部副作用 exactly-once、远程文件系统锁语义、
断电/磁盘损坏恢复或真实长期运行。旧版本 worker 不参与新锁协议，升级时须先全部停止。

当时的代码复核：`MobilePage.tsx` 的控制/保活开关只写浏览器本地设置，
不构成认证、设备绑定、推送或系统保活实现（下节修复已移除这些开关）。
FR-25 不能因存在该页面或局部配对功能而标为全部完成。
`ingest.py` 和 `feedback.py` 已有部分代码，不能沿用历史审计的“完全未实现”描述，
上表已据现有实现细化其剩余缺口。

## 2026-09-05：可信移动设备、摄取与反馈闭环

代码交付：`b7f6739`；交付前基线：`7331138`。新增的本地实现与配置说明：

- [移动 companion](mobile-companion.md)：明确项目范围、十分钟一次性配对、双端
  六位验证码批准、批准后轮换私有 cookie、三十天到期和撤销；手机只可单次审批，
  是否允许发起任务由桌面单独授权，不获得后台管理身份。移除虚假 OS 开关。
- [PDF/图片摄取](ocr-ingestion.md)：正确的可选本地 PaddleOCRVL 适配，受限子进程、
  失败/缺页/截断说明、pypdf/Tesseract 真实降级；上传原件 Read 指向 Markdown，
  不限制正常生成媒体/截图读取或受沙箱约束的二进制加工。没有下载模型或冒充 VL 成功。
- [反馈闭环](feedback-loop.md)：消息/证据服务端精确锚点与用户 why，失败/取消
  幂等采集和启动补采，人审处置历史及版本冲突、显式无标签候选导出/抽样。
  Skill 折叠记录只承载人的工作确认，不自动编辑 Skill，不伪造 gold 或 L2 认证。

交叉复核发现并修复：读取慢请求后复用已撤销设备权限、批准配对时绕过当前签名角色、
重复审批制造长期 grant、非 owning worker 误报任务已取消、旧反馈响应覆盖新 revision、
动态分页重复页卡住，以及脱敏扩长导致合法反馈/证据超限。跨进程配对竞争只有一个成功；
非 owning worker 的取消返回 HTTP 409，不改变仍在执行的 Run。

根代理在全部代码冻结后验证：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：728 passed，8 live-only skipped。
- `.venv/bin/ruff check backend/src backend/tests evals/tools`、`git diff --check`：通过。
- `.venv/bin/python -m pytest evals/tools/tests -q`：59 passed。
- `PYTHONPATH=evals/tools .venv/bin/python evals/tools/run_snapshot_evals.py`：61/61。
- 前端 `npm run test:run`：137 passed；`npm run lint`、`npm run build`：通过。
  主 JS 581.64 kB，既有 >500 kB 警告仍在，没有通过调高警告阈值掩盖。

验收边界：未发起本轮真实外发、付费模型调用或大型模型下载。配对/API 测试使用
隔离账本和离线 runtime，不能冒充真实手机 TLS/E2E。浏览器验收已尝试连接，但当前
Browser 插件运行时引用已不存在的版本路径，连接失败；没有绕过该工具边界或声称目视通过。
真实 VL/版式质量、TextIn、生产 OCR 解码隔离、Web/native push、跨进程持久审批等待、
组织物理隔离及其余上表 deferred 保持开放；全 PRD 目标尚未完成。

## 2026-09-05：持久审批、Web Push 与任务经验

新增交付合同：[持久审批](durable-approvals.md)、[Web Push](companion-web-push.md)、
[Schedule 任务经验](task-experience.md)。保持同一主 Agent 与原生 SDK 工具执行。

- 原生审批确认后暂停并退出 CLI，另一个控制平面可保存决议并恢复原 Run/session/tool；
  审批消费跨进程原子化，重放、过期、撤销与不确定结果不能绕开 checkpoint。
  真实 SDK 探针发现并修复 AskUserQuestion 输入交接与完成审计；分支 stream 不再污染
  主 Agent 的完整工具批次。恢复重新核验成员、Provider 资格/配置和原预算/墙钟。
- 可选 Web Push 接入私有订阅、VAPID 与加密、当前设备/项目权限复验、跨进程投递租约、
  有界尝试和设备级持久 Retry-After；HTTP-date、通知取消/过期及订阅替换不会提前重试。
- 任务经验独立于全局 Memory 和用户策略；来源绑定 Run/trace/session 的实际工具事件，
  dispatch 配置指纹与 prompt 同快照，取消/配置变更不能写入旧经验，人工可按版本停用。
- 前端修复迟到续接响应覆盖 SSE 完成状态、停用冲突后复用旧版本确认、拒绝被误称批准。
  SDK 分段用量累计保留未知成本语义，避免崩溃/缺失字段被当成零消耗。
- Green Path/L2 驱动使用受信本地 Host；审批以当前 pending API 为准，409重新读取后重试，
  已保存决议经恢复接口继续，其他 HTTP 失败不能冒充成功。原 prompt 与验收器未降级。

本批代码冻结后的验证（现金流是下一项独立开发，不在本批范围）：

- 后端 `pytest backend/tests -q --ignore=backend/tests/test_cashflow.py`：**923 passed / 8 live-only skipped**。
- 前端 **188 passed**；lint、TypeScript、build通过；主包607.89 kB，既有体积警告保留。
- Ruff、`git diff --check`通过；eval tools **59 passed**；Snapshot **61/61**。
- 固定SDK 0.2.143的4个真实CLI离线集成场景通过并归档至
  `evals/reports/native-approval-20260905/`；另有真实两个spawn进程争用审批/消费测试。

代码提交：`96d8062`。真实 DeepSeek Green Path 已完成下面的三轮重验。真实手机浏览器验收
尝试返回 `No browser is available`，Browser列表为空；没有绕过工具边界或声称目视通过。
原生手机推送、外部Push服务、系统防休眠、长期运行及其余 deferred 继续保留在全量目标中。

### 同日真实 DeepSeek Green Path 三连过

归档：`evals/reports/greenpath-20260906T044246Z/`（UTC日期09-06，当地09-05）。
固定 SDK 0.2.143，服务模型 `deepseek-v4-pro`，三轮全部 passed；每轮13个SDK turn，
Read/Web/Skill/MCP/Bash/Write/Edit、持久审批拒绝后继续、同 Run/session 来源收据、
SSE精确序号回放和会话续接均通过，missing列表为空，Web未降级。
三轮主任务SDK标价估算分别为1.781746、1.763105、1.778922美元；这不是厂商实际账单，
也不含另行会话续接Run的费用。审批恢复没有重置每个逻辑Run的累计用量。

命令：`PYTHONPATH=backend/src .venv/bin/python -u backend/scripts/run_green_path.py --provider deepseek --rounds 3 --budget-usd 2 --timeout-s 900`。
初次调用被旧测试Host `greenpath` 的本地来源检查拒绝，尚未创建项目或调用模型；失败记录
保留在 `greenpath-20260906T044218Z/`。修正验收脚本为合法loopback Host后完成上述三连过，
没有放宽应用鉴权、改写prompt或降低验收器。

这些是合成商业材料上的真实模型/工具路径，不是真实商业Connector外发、Deep Research
质量、手机HTTPS/Push、生产多租户或人工gold/judge校准验收；对应deferred继续开放。

## 2026-09-05：现金流计算与内置 Skill 资源

新增 [确定性现金流 MCP](cashflow.md)：用显式同币种结算计划计算现金余额、日末资金
缺口和当天先付款后收款的保守缺口。Decimal 固定四位小数、最多500笔与3660天，
最低保留现金不列支出，已有融资不重复计算；完整规范化输入与指纹允许复算，引用
始终未验证。修复来源清洗为空白后被错误计为有来源、返回输入无法复算的问题。

13个内置 [Skill 资源包](skill-resources.md) 均有输入/证据参考与可编辑模板，并随
不可变包一起固定。原生 Read/Glob/Grep 仅增加本次实际 Plugin 的只读 Skill 目录；
启动审批 bridge 使用同一范围。固定方法的读取结果保留指令语义，项目资料仍降权为
数据；加载固定 Skill 沿用 SDK 自动批准，后续工具动作分别受权限控制。

代码冻结后验证：

- 后端 `pytest backend/tests -q`：**995 passed / 8 live-only skipped**。
- 54项现金流专项测试；13包真实快照测试覆盖资源链接、包更新不改旧快照及模板副本编辑。
- 固定 Skill 原生文件权限/结果信任测试覆盖跨快照、禁止写回与符号链接逃逸。
- 独立审查穷举54组两日结算顺序，现金流最坏资金需求与整数 oracle 一致。
- Ruff、`git diff --check`通过；eval tools **59 passed**，Snapshot **61/61**。

本批未改前端。真实 Provider 的现金流/参考读取专项验收另行归档；上述离线验证不
证明真实商业输入、回款预测、融资执行、Skill 回滚重跑或备份恢复。全量目标继续开放。

代码提交：`99374ac`，已推送。真实现金流验收脚本另有18项离线校验器测试通过；
实际启动被自动审批审查拒绝，原因是缺少向 DeepSeek 发送本地 Skill/上下文和合成
fixture 的明确授权。已请求批准；`cashflow-e2e-blocked-20260906T051241821334Z` 记录
`execution_started=false`，没有模型开销，也没有把未执行验收标为通过。

## 2026-09-05：本地控制面性能基线

[测量合同与结果](control-plane-performance.md)，原始归档
`evals/reports/control-plane-20260906T051626Z/`。实际FastAPI中间件/路由、FileLedger、
13包SkillRegistry与RunManager；只将模型运行替换成显式离线等待任务。正式样本单
并发100、4并发100，各另留10个预热；220个原始样本零失败，测量期间源码指纹不变。

| 正式P95 / ms | 单并发 | 4并发 | PRD门槛 |
|---|---:|---:|---:|
| 创建API | 11.158 | 42.108 | <500 |
| 首排队/启动事件持久化 | 9.758 | 35.081 | <2000 |
| 离线任务取消生效 | 4.272 | 15.281 | <2000 |

根代理独立复算全部原始时间公式、12个P95及归档SHA-256，均与报告一致。
测量器11项回归和Ruff通过，覆盖完整生命周期、时间点、失败分母与异常归档留存。
仅一次正式基线；不调整阈值、不暂停同机负载、不重复选取最快报告。

边界：ASGI进程内运输、local_only、每组一个增长中的项目、不启动lifespan后台服务；
没有SDK/模型/Provider、socket/TLS/代理/SSE客户端、多worker或生产负载。
首事件持久化不等于客户端已收到；离线task退出不等于SDK子进程中断。相应验收继续开放。

## 2026-09-05：项目运行指标与工作区统计

[项目指标合同](project-metrics.md)：有界只读API与近7/30天统计面板，复用既有事件和
RunTelemetry，不新增日志管道、不调用模型。项目/当前成员范围在读取前后重新核验，
按创建时间选窗并说明遗漏、超限/损坏日志、未知标识和无法配对的工具事件。

Provider取冻结快照，模型取SDK已报告标识；真正开始的Skill/MCP调用才计入。
每Run只计最新累计成本，未知费用不当零，不向多个参与模型/Skill重复分摊全Run费用。
MCP camelCase/snake_case错误标记都严格解析，冲突/非法值归未知；分位数保留样本覆盖，
运行完成不代表业务有用。前端按需加载、撤销旧请求，切项目和403刷新不会泄漏旧结果。

完整冻结验证：后端 **1112 passed / 8 skipped**；前端 **201 passed**；Ruff、ESLint、
TypeScript、build、diff-check通过。主JS619.11kB，既有bundle体积警告保留。
含真实非空ASGI路由的项目费用/隐私隔离，以及日志预算、撤权、重复事件和未知数据回归。
Browser验收尝试返回 `No browser is available`，列表为空；未声称目视通过。
真实SDK现金流仍等待向DeepSeek发送具体载荷的批准，其余全量deferred保持开放。

## 2026-09-05：手动与自动调度的重叠治理

代码提交：`b0a063d`，已推送。

[调度合同](schedule-overlap.md)：两类入口共用非阻塞跨进程dispatch租约，创建Run期间
持锁；释放后由全部绑定同一项目/计划的持久非终态Run继续排他。waiting_permission、
paused、cancelling不会误判为已结束；成功与重叠409的手动请求保留计划时间和暂停状态。
自动到期仍按既有规则跳过重叠，不重复已存在的occurrence；配置/权限失效仍暂停计划。

页面按计划管理待处理操作，同计划Pause/Run互斥，不同计划独立解锁；迟到成功不会
覆盖最新冲突。两种409都有中英文提示，旧成功提示会清除，不自动重试。

本批冻结验证：后端 **1135 passed / 8 live-only skipped**；前端 **206 passed**；
Ruff、ESLint、TypeScript、build、diff-check通过，主JS619.72kB既有体积警告保留。
新增18项API/同进程边界与5项真实双spawn进程场景，覆盖持锁时、Run持久化后、
dispatch返回后以及两种进程终止边界的恢复。模型为离线替身，无外部调用。

本项不提供HTTP请求幂等键，终态后的新手动请求仍创建新Run；远程文件系统锁、磁盘
损坏、外部副作用exactly-once、真实Provider长期任务及渠道验收继续开放。当前Browser
不可用，没有新增目视验收声明。Skill备份恢复演练另行交付，不在本批验证范围。

## 2026-09-05：Skill 本地独立备份恢复演练

代码与归档提交：`06f3c94`。[演练合同](skill-backup-exercise.md)，正式报告
`evals/reports/skill-backup-20260906T064155970322Z/`；该目录的`root-audit.json`记录
根代理独立复算。测量时HEAD为`b0a063d`，脚本/Registry/模型/协议源码指纹全程稳定。

合成源完整备份含176个文件、302,997字节、16个历史包、15个当前Skill；包括禁用、
非current、未进入历史的应用Skill和旧enabled override。临时源根移走并删除后，
6个全新进程完成一次恢复与5种故障注入，没有读取实时应用配置、项目、凭据或调用模型。

成功恢复逐个扫描16历史包和13应用Skill；旧43/新40个运行文件在staging及最终目标
都与源基线逐字节一致，回滚保持完整canonical状态。缺旧历史、文件篡改、坏current
pointer、哈希仍完整的危险旧包、复制中断各命中指定原因/异常类型/阶段，失败目标
不可见且staging已清理。根代理另复算全部文件哈希、16个包地址、25份固定包副本及
6个进程报告，不依赖脚本自己的验证函数。

边界测试包括FIFO非阻塞拒绝、符号/硬链接与祖先、目标在预检后出现时保留其inode、
空快照/全部禁用/空历史、物化工作上限、完整override与发布后失败的诚实报告。
本批最终后端 **1182 passed / 8 live-only skipped**；备份专项 **47 passed**；
Ruff、diff-check通过。未改前端，沿用上批 **206 passed** 和lint/build验证。

本项是同主机、同相关源码版本、私有静止目录中的本地演练；macOS原子发布已实测，
Linux分支未实机验证。清单不是签名/来源认证；生产备份、加密/异地保留、跨宿主恢复、
灾备指标、HTTP鉴权/真人审批流程和真实Provider回滚重跑仍未由本报告证明。
FR-12及全量PRD的这些剩余要求继续开放；真实DeepSeek现金流仍等待具体外发载荷授权。

## 2026-09-06：证据/决策事件与完整 Run 流回放

代码提交：`40ac606`。[事件与恢复合同](project-events.md)覆盖 §4.12 中有真实 Run
来源的 `evidence.added`、`decision.recorded`。通知读取已提交的业务记录，不复制正文或
项目状态；重复请求在原属 Run 补发，以同一跨进程日志锁保证资源通知幂等和连续序号。
批量部分提交、浏览器 Evidence 成功但 Artifact 失败、摘要写失败及进程崩溃都保留
原业务结果；运行结束、启动恢复和订阅/收尾时可以只补通知，不重跑模型或业务操作。

SSE 等待真实执行租约释放（包括其他进程），完成补发并发送全部尾部后才发出无 id、
不入账的 `stream.completed` 水位。无效/跨 Run/未来游标拒绝，投影失败不确认完成；
流中逐项复验成员权限和身份断言有效期。前端只接受生命周期状态，验证连续序号、
去重回放并保持终态尾部重连；业务通知触发授权 GET，合并刷新且隔离切项目的迟到结果。
实际工作区回归覆盖新增 Evidence/Decision 显示、历史审批不复活以及读取失败提示。

Green Path 按完整帧验证结束水位，订阅结束后读取最终账本。验收重放设置总时限，
持续心跳不能无限延长等待；缺帧/错误水位/503/超时都保留完整事件与一致的 FAIL 报告，
不启动后续付费续跑。该验收器改动只做离线验证，不借用此前真实报告声称新协议已真测。

最终冻结验证：后端 **1291 passed / 8 live-only skipped**（47.21s）；前端
**225 passed**；Ruff、ESLint、TypeScript、build、diff-check通过。沿用既有Vite
体积提示（主JS622.69kB）。新增测试包括真实spawn争用/终止恢复、实际native MCP请求、
ASGI流回放/权限变化，以及无真实Provider的完整验收脚本归档检查。

边界：没有新增真实Provider或真实浏览器EventSource验收；远程文件系统、跨宿主、
网络代理/生产负载仍未验证。记录修改、无Run归属的项目变更、`artifact.updated`、
`memory.proposed`、schedule/preflight细分继续deferred。Skill提案人审等其他全量PRD
要求保持开放。真实DeepSeek现金流仍因自动审批审核要求明确外发载荷/目的地授权而等待。

## 2026-09-06：Skill 自创建提案、人审与发布恢复

[Skill 提案契约](skill-proposals.md) 接通主 Agent 原生 Write/Edit 与现有
`register_artifact`。仅显式登记保留路径的版本化 manifest 会提交候选；普通文件、
上传和运行收尾扫描不会提交。来源绑定同项目真实 Run/Artifact/已初始化 SDK session；
历史 Run 引用可选，宿主不以重复次数或业务关键词代替 Agent 和人的判断。

候选 manifest、SKILL.md 与资源冻结到独立只读包，受路径、软硬链接、特殊文件、大小、
条目和总量配额检查。初始 pending 不进入运行时 Plugin；静态扫描通过仍须人审，
失败候选可拒绝但不能强制发布。独立 Proposals 页提供来源、扫描、冻结文本预览、
二进制原件下载、人工确认和完整审阅记录；发布后保持禁用，既有 Run 保留固定快照。

人审绑定 revision、包哈希和当前目标的 hash/enabled/generation，防止旧审批覆盖
升级、回滚或启停。发布前在 Registry 复扫、归档之后再次核验当前项目管理员及断言
有效期；撤销或过期不提交。canonical schema 2 原子保存选择、禁用状态和发布凭证，
重试使用精确凭证。进程中断后，有凭证只修复候选状态，没有凭证回到 pending 新版本
并要求重新人审；不自动重跑发布，也不覆盖后续启用或回滚。

HTTP 先只读确认项目权限再进行恢复；隐藏候选不会因无权请求发生持久变更。无法确认
范围的坏元数据在列表中跳过并记录安全错误类型，不拖垮其他项目列表。预览与下载核验
整个冻结包及文件哈希，并在返回前复验权限。前端处理候选切换、迟到响应、分页版本、
409 后重新确认、403/404 清空，以及旧安装操作不能覆盖新发布状态。

最终冻结验证：后端 **1470 passed / 8 live-only skipped**（58.12s）；前端
**260 passed / 25 files**；Ruff、ESLint、TypeScript、Vite build、diff-check 通过。
测试包括生产 FastAPI/原生 MCP 分发、真实 spawn 并发和进程终止、提交前权限撤销/过期、
候选字节攻击、Registry schema 1/2 兼容及发布凭证备份保留。沿用 Vite 大于 500 kB 的
主包提示；浏览器不可用，未新增可视验收或真实 Provider 提案质量声明。

边界：静态检查不是恶意代码或业务质量的完整证明。旧正式 Skill 备份报告仍只对应
当时 schema 1；本次 schema 2/receipt 备份兼容仅经 pytest 验证，候选库与项目账本的
一致备份、真实模型提案质量、生产控制面/worker/租户隔离仍 deferred。
没有新增 MCP 工具，也未绕过待批的 DeepSeek 外发验收。

## 2026-09-09：含发布凭证的 Skill 独立恢复验收

代码与正式归档提交：`73d307d`；基线：`b34fbd7`。
本批延续 FR-12，补齐上批仅经 pytest 覆盖的 schema 2/receipt 正式恢复演练。
实现与证明范围见 [备份恢复合同](skill-backup-exercise.md)。

| 本批要求 | 状态与证据 |
| --- | --- |
| 在正式备份中保留 schema 2、四份发布凭证、generation 和启停选择 | delivered：Registry 实际发布方法产生合成凭证；源目录删除后从独立副本恢复，canonical 字节一致 |
| 恢复后旧、新运行快照和已发布历史可用 | delivered：16 个历史包、13 个当前应用包扫描；旧/新 43/40 个文件在 staging 和最终目标均逐字节一致 |
| 旧凭证重试不覆盖后来的发布、回滚与启停 | delivered：四次匹配重试不读取不存在的候选、不产生新提交且状态字节不变；16 次单字段绑定变化被拒绝 |
| 凭证缺失、绑定变化和无对应历史包不能被默默接受 | delivered：重新计算运输清单的三项故障命中逻辑 inventory 或凭证历史关联检查；连同原五项故障均无目标发布、staging 清理完成 |
| 真实浏览器提案审阅验收 | deferred：按 Browser 技能连接及故障发现流程检查，返回 `No browser is available`，可用列表为空 |
| 提案候选库、项目账本与 Registry 的一致应用备份 | deferred：本批限定 Registry/历史/pins，尚未建立完整应用快照与恢复协议 |
| 生产/跨宿主恢复及真实模型提案质量 | deferred：需要独立部署、授权配置和相应验收；本地合成演练不替代 |

归档：`evals/reports/skill-backup-20260909T091922465490Z/`。独立备份 176 个文件、
305,589 字节；九个不同新进程完成一个成功场景与八项指定故障，全部达到预期。
另用标准库独立复算 176 份文件哈希、16 个包地址、25 份 pin 包副本，核对四份凭证
与九份报告，未调用被验收脚本或 Registry 验证函数。复核仅针对保留的归档，临时
恢复目标已被演练清理。清单不提供签名、真实审阅人认证或对恶意同时改写清单的防护。

验证命令与结果：

- `PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests/test_skill_backup_exercise.py`：**59 passed**；包括验收器拒绝状态被改变、错误绑定被接受、凭证读取丢失与空凭证 fixture。
- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：**1478 passed / 8 live-only skipped**（68.99s）。
- `PYTHONPATH=backend/src .venv/bin/python backend/scripts/exercise_skill_backup.py`：**passed**。
- `.venv/bin/ruff check backend/src backend/tests backend/scripts/exercise_skill_backup.py`、`git diff --check`：通过。

本批未改产品前后端行为，未调用真实 Provider、读取正式数据、发送外部消息或下载模型；
前端未改且未重跑其自动化检查。其余全量 PRD deferred 保持开放。

## 2026-09-09：项目账本、提案与 Registry 的一致应用冷备份

代码与正式归档提交：`aeba9fc`；基线：`bf161e8`。本批完成上节尚未交付的本地应用快照与恢复协议，
操作与限制见 [应用冷备份合同](application-backup.md)。

| 本批要求 | 状态与证据 |
| --- | --- |
| 同一快照保留项目账本、提案冻结包/审阅和 Registry/凭证 | delivered：完整数据目录与应用 Plugin 同批复制，源目录删除后的独立进程恢复；其他存储保留原始字节 |
| 冷备份不与应用初始化、运行或关闭恢复竞争 | delivered：应用共享/备份独占进程租约；真实 spawn、构造与 lifespan 检查；非终态 Run 拒绝备份 |
| 恢复关联完整且不重新发布 Skill | delivered：逐项验证项目/Run/session/Artifact、冻结包、固定版本及凭证；两个 publishing 中断窗口只按凭证修复记录，canonical 字节不变 |
| 错误快照与并发目标不产生部分覆盖 | delivered：重新计算清单后的语义错误、源变化、额外恢复写入、路径/软硬链接/FIFO/合计配额均拒绝；新目录独占原子发布 |
| 归档可保留权限并独立复现 | delivered：标准库独立复核；压缩归档保留只读位和空目录，解包后另一个全新 CLI 进程恢复成功 |
| 项目级授权导出/删除与恢复 UI | deferred：本批入口是本机操作者 CLI，尚未提供 HTTP 授权与页面操作流程 |
| 在线/跨版本/跨宿主/生产灾备与所有子系统语义认证 | deferred：本批为同代码版本、停机、本机私有目录恢复；外部密钥/服务、SDK 跨路径续接、加密异地保留及灾备指标需另行实现和验收 |

正式合成归档：
[application-backup-20260909T095925318909Z](../evals/reports/application-backup-20260909T095925318909Z/summary.json)。
快照含 146 个文件、165 个目录、192,446 字节、一个项目/Run、五份提案、两份发布凭证
和 13 个内置 Skill。源目录删除后，五个不同新进程覆盖成功恢复、缺候选、缺凭证、
Run/session 来源错误与文件篡改。成功结果为两个 pending、两个 published、一个 rejected；
两类 publishing 分别按已有凭证补记和无凭证退回待审，既有 canonical 与备份均未改写。

验证命令与结果：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest -q backend/tests`：**1513 passed / 8 live-only skipped**（70.89s），包含新增 **35** 项应用备份测试与既有动态权限/调度/SDK 回归。
- `PYTHONPATH=backend/src .venv/bin/python backend/scripts/exercise_application_backup.py`：**passed**，父/子进程均阻断网络与正式数据访问。
- 标准库独立复核：[independent-check.json](../evals/reports/application-backup-20260909T095925318909Z/independent-check.json)；保留权限的压缩包解包后新进程恢复：[archive-roundtrip.json](../evals/reports/application-backup-20260909T095925318909Z/archive-roundtrip.json)，均通过。
- `.venv/bin/ruff check backend/src backend/tests backend/scripts/exercise_skill_backup.py backend/scripts/exercise_application_backup.py`、`git diff --check`：通过。

本地预览后端已加载新版本，健康检查正常；运行中的实际应用持有共享租约，独占备份锁
被正确拒绝，未对正式业务目录执行备份。未改前端，也未新增备份 UI 或真实 Provider 验收。
全量 PRD 其余 deferred 保持开放；之前各节的 deferred 表示对应交付时点，以上两张总表
和本节反映本次应用冷备份的新增范围。

## 2026-09-09：项目参考资料知识库

交付合同：[项目知识库](project-knowledge.md)。项目文件与产物账本按当前登记版本
生成只读检索视图，中英文关键词、文件名和正文联合检索，返回原文行列、Artifact
id/版本/hash/时间/Run/来源关联。PDF/图片使用已有 Markdown 伴随文件，命中处同时
保留摄取头部，提醒缺页、降级及原件核对要求。检索不会创建证据、记忆或自动化承诺。

工作区增加中英文检索、文件覆盖情况、来源详情、刷新和下载。API/MCP共用有界读取，
拒绝隐藏路径、符号链接、硬链接、特殊文件、读取中变更与登记校验和不符的内容。
专用下载发送再次校验后的字节副本；读取及下载前后均复验项目权限。文件变化、格式
不支持、读取总量/数量限制均可见；迟到响应或项目切换不会复用旧项目的结果。

最终代码冻结后的验证：

- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：**1539 passed / 8 live-only skipped**（77.98s），包含新增26项知识库/授权/下载测试。
- 前端 `npm run test:run`：**269 passed**；`npm run lint`、`npm run build`通过。
- `.venv/bin/ruff check backend/src backend/tests`、`git diff --check`通过。
- eval tools **59 passed**；Snapshot **61/61**，既有验收器未降级。
- 主JS包652.15kB，既有大于500kB构建提示保留。

Browser运行时已成功加载，但选择浏览器返回`No browser is available`，按文档排查后
列表为空，未声称页面目视验收通过。本批未调用真实Provider或外部数据服务。
Office原件索引、真实检索质量/容量、页面目视验收和全PRD其余deferred继续开放。

## 2026-09-09：五平台接入依据与 TikTok Shop 管理员只读流程

[接入合同与边界](marketplace-connectors.md)记录用户提供的 SHEIN、Temu、TikTok Shop、
AliExpress 和 Shopee 官方文档。Alibaba 托管的链接归属速卖通；既有 `alibaba` 与
TikTok 社媒凭据不迁移、不混用。其余四个平台在页面明确显示“适配器待实现”。

本批 delivered：TikTok Shop 独立 Catalog 记录、结构化 seller 凭据存储、到期时间
校验、两项固定官方只读接口、每次商品查询重新核实 shop 授权并在服务端解析 cipher、
分页游标、来源路径/读取时间/request ID、中英文配置与读取/撤销/重连页面。
控制面只允许管理员手动读取，不自动创建证据或项目真值。传输使用固定官方主机、
公网 DNS 固定地址、TLS/Host/SNI、30秒总期限及2MiB响应上限；撤权/轮换/过期复验，
拒绝重定向，签名URL及上游错误正文不进入应用响应。

新增53项后端测试与5项前端测试，覆盖签名向量、实际发送正文/头、游标、私网拒绝、
店铺授权、读取中撤销/轮换/过期、限长与响应关闭、凭据反射、真实FastAPI管理员边界、
凭据表单与分页交互。全量后端 **1592 passed / 8 live skipped（74.93秒）**，前端
**274 passed**；Ruff、ESLint、生产构建通过。评测工具 **59 passed**，Snapshot
**61/61**。构建仍提示主JS超过500kB（本批660.93kB）。

本批没有使用真实商家凭据或发送真实平台API请求；官方文档核查不等于平台认证。
Browser重新检查仍返回空可用列表，未声称视觉验收。TikTok Shop OAuth/refresh、
Agent项目授权绑定、来源凭证入库、真实test-shop与商家授权联调、写操作，以及其余
四个平台适配器继续 deferred；完整PRD目标保持开放。

## 2026-09-09：项目指定店铺授权、主 Agent 读取与来源凭证

交付前基线：`383ceb2`。本批补齐上节的 Agent 项目授权绑定和来源凭证接入；
使用方式、API 和边界见 [电商平台接入合同](marketplace-connectors.md)。

管理员在连接器页面选择项目、已核验店铺和 1/7/30 天有效期，创建或续期只读授权。
授权存于项目工作区之外，保留创建/撤销人和时间；revision CAS 与跨进程锁保护更新，
续期生成新绑定并停用旧绑定。创建前重新核实 seller token 的店铺列表和配置的商品
权限，网络返回后再次核验管理员资格。同 app 更新 token 可沿用有效授权，换 app
须重新授权；连接器停用时仍可撤销项目授权。历史最多保留 1,000 条，撤销不删除来源。

既有 ledger MCP 增加主 Agent 的 `read_marketplace`：只列本项目已授权店铺，按绑定
读取每页最多10个商品。项目/Run/session/principal由宿主绑定；每次读取复验当前身份、
Run状态、SDK session、店铺授权及连接器状态。原生SDK权限与PreToolUse均拒绝研究支线
自行调用；主Agent可向支线提供已读取的观察。SDK preset、严格MCP配置和单主Agent架构保留。

来源凭证由服务端签发，绑定同项目/Run/session/principal及清洗后的实际商品页，
有效期不超过15分钟且不超出店铺授权剩余时间；既有 `record_evidence` 可引用、核验
和回放。固定结构数据保留每字段4,000字与总计32,000字上限，超量整页拒绝；保留
JSON形式的字面商品标题，移除伪造指令边界。原生MCP分发与PostToolUse替换后的内容
逐项匹配凭证，覆盖空页、10条长标题和JSON文本，避免先签发再改变Agent所见内容。

验证命令与结果：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：**1620 passed / 8 live-only skipped**（73.68秒）。新增28项后端测试，含生产FastAPI权限、读取中撤销/过期/取消/session及app变化、跨项目拒绝、同app令牌更新、并发授权、真实双进程撤销CAS与来源入证据链路。
- 前端 `npm run test:run`：**278 passed / 28 files**，新增4项项目授权交互测试；全量字典覆盖检查发现并补齐“操作”表头。
- Ruff、ESLint、TypeScript、Vite生产构建及diff-check通过；主JS包666.76kB，既有大于500kB提示保留。
- `.venv/bin/python -m pytest evals/tools/tests -q`：**59 passed**；Snapshot **61/61**，既有验收门槛未改变。

本批未使用真实商家凭据，未调用真实平台API或Provider。Browser可用列表仍为空，
未声称界面目视验收。真实模型使用此工具、真实test-shop/商家权限与区域/限流验收、
OAuth交换和自动refresh、商业写操作以及其余四个平台adapter仍deferred。
商品列表仅代表授权商家的自有数据，不支持全市场需求或竞争对手销量结论；
全量PRD目标保持开放。

## 2026-09-09：店铺身份、独立登录浏览器、任务采集与数据展示

交付前基线：`0b1b9c4`。用户明确的两层结构已落到实现：平台 Connector 管应用凭据与
API；项目店铺保存身份及可选 Connector 关联，独立浏览器保存登录会话。完整使用说明、
接口及限制见 [店铺浏览器](shop-browsers.md)。API 与浏览器授权分别管理，尚未自动合并两路数据。

本批 delivered：每项目最多100个店铺、独立持久profile及窗口进程、用户本人登录/确认、
按当前会话到期/撤销的授权；现有SDK浏览器MCP支持按shop_id读取、基于观察引用操作、
逐次审批报表下载。页面截图、文本和原始报告通过真实Run保存为来源凭证、Evidence与Artifact，
前端列出来源和可下载文件，页面文本进入项目知识检索。没有另造模型循环或固定子Agent。

私有socket和进程租约确认窗口所有权；排队执行、运行期间、网络检查和落账前复验Run、
SDK会话、成员、店铺revision、授权和组织策略。撤销/取消/断线中止进行中的操作，已发出
的远端副作用不承诺回滚。登录文件处于工作区之外，并加入SDK沙箱禁止读写范围。
活动浏览器持有维护租约，阻止冷备份复制变化中的profile。真实浏览器测试复现了截图临时
修改input样式导致观察引用误判，修正为截图完成后绑定元素，保留真实DOM变更拒绝。

最终验证：

- 后端全量：`OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/pytest backend/tests -q`，
  **1630 passed / 13 skipped**，70.80秒；其中8项真实Provider测试、5项独立Chromium测试由环境门控。
  此后新增4项文件篡改拒绝用例，所在权限测试模块独立运行 **13 passed**。
- 真实Chromium离线验收：`OPHIRA_SHOP_BROWSER_E2E=1 PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_shop_browser.py -q`，
  **6 passed**，3.33秒，包含一个不依赖Chromium的范围测试。使用合成HTTPS页面、真实DOM、
  下载事件、独立profile、socket及完整来源/文件回放；原生MCP和PostToolUse内容逐项一致。
- 前端全量 **283 passed / 29 files**；新增5项身份确认、编辑CAS、操作互斥、数据下载展示及项目切换测试。
- Ruff、ESLint、TypeScript、Vite构建及diff-check通过；评测工具 **59 passed**，Snapshot **61/61**。
  主JS包678.50kB，既有大于500kB提示保留。

真实Chromium保留自身沙箱；本机执行沙箱阻止启动时，在获准的执行环境重跑离线测试。
Browser工具重新选择和发现均无可用浏览器，未宣称前端目视验收。未使用真实卖家凭据、
未调用真实平台或Provider。OAuth/Token交换与刷新、其他平台API、真实登录/验证码/风控
及平台报表适配、实际模型任务与生产资源/隔离认证继续deferred；完整PRD目标尚未完成。

## 2026-09-09：不可变产物版本与文本编辑

交付前基线：`5435cb0`。新增合同：[产物版本与编辑](artifact-versions.md)。

复核发现历史记录仅保存元数据，旧版下载仍指向可覆盖的工作文件；A→B→A 内容回退
返回旧版编号，跨进程登记可能重复分配版本。新登记通过描述符捕获、SHA-256 及文件
大小校验，将不可变副本先持久化再发布记录。项目锁覆盖版本分配，修改/删除工作文件
后仍能准确下载历史版本；无法验证的旧数据明确不可用，不用当前文件冒充原件。

工作区新增中英文 Project files，支持完整路径列表、搜索、逐版本下载和来源审查。
512 KiB 内 UTF-8 文本可原位编辑、比较修改并保存新版本；校验基准版本/工作文件/目录
身份，保留旧字节、编辑者和前版来源。运行中的任务（包括暂停和终态清理）阻止人工
覆盖；新任务创建与编辑共用项目锁。HTTP 有界读取并在 I/O 和替换前复验权限，失败
保留草稿，同请求重试幂等，跨项目导航丢弃迟到读写响应。新版本进入现有项目知识库。

编辑意图先落盘；进程退出后只根据当前字节补记原版本，不重放文件修改。覆盖交换前、
交换后和账本提交后的中断；另有真实 spawn 进程交换后直接退出、2/4 个进程同时登记
以及新目录冷备恢复测试。备份验证新增副本完整性检查，损坏历史不能通过验收。

代码冻结后的验证：

- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1670 passed / 13 skipped**，72.74 秒；跳过8项真实Provider及5项独立Chromium环境门控测试。
- 产物/编辑/API/进程/应用备份专项 **71 passed**，包含真实 OS 进程竞争与退出。
- 前端 `npm run test:run`：**292 passed / 30 files**；新增9项文件管理交互回归。
- Ruff、ESLint、TypeScript、Vite构建和diff-check通过；评测工具 **59 passed**，Snapshot **61/61**。
  主JS包688.74 kB；既有大于500 kB提示保留。

本轮未调用真实Provider、平台账号或外发服务。Browser连接返回无可用浏览器，按文档
发现列表也为空，未宣称目视验收。真实模型连续编辑、Office渲染/布局、Bash修改的完整
工作区恢复、生产隔离，以及上表其他deferred均继续纳入全量目标；完整PRD尚未完成。

## 2026-09-09：运行前工作区快照与人工确认恢复

交付前基线：`a170c22`。合同与容量边界见 [工作区检查点与恢复](workspace-recovery.md)。

每个新 Run 在进入 SDK 之前冻结完整的可支持文件树，包含未登记文件、隐藏文件、
空目录、二进制内容与所有者权限；因此覆盖 Bash 写入后的文件恢复。原生 SDK 文件
检查点继续启用。同项目执行和终态产物收尾共用跨进程租约，其他项目可同时执行；
排队取消不会冒领另一 Run 的文件。检查点线程取消后完成清理才释放租约。

中英文工作区新增恢复点列表、全部变更分页预览、替换/移除数量、明确确认、结果查询
及同请求重试。恢复绑定项目/审阅者/session、十分钟有效期与两棵树指纹，权限在最终
读盘后再次复验。先冻结并保留当前文件，再按已持久化授权进行两次不覆盖目标的目录
切换；重启修复不创建假的空工作区，不覆盖不明新目录。产物版本、项目真相、对话和
商业收据保持历史，恢复不会重放 SDK 或商业动作。

实际 `/bin/sh` 改写/删除/新建文件后可恢复原树。真实 spawn 进程在授权发布、两次
目录切换、产物登记和 applied 发布后直接退出，重启均恢复同一次已授权操作；另外
验证未知替代目录拒绝、后续人工编辑保留、跨进程运行争用、过期/撤销/过时预览、
损坏/链接/特殊文件、取消快照线程、文本编辑意图与工作区恢复衔接、完整冷备往返。
冷备验证检查点引用和 blob；待恢复目录切换须先在原宿主完成，不跨目录重放 inode。

本批不包含真实 Provider/SDK 故障注入、Linux 实机与网络文件系统、生产灾备、历史
保留管理和大容量性能认证。Browser 发现列表仍为空，未新增浏览器目视验收；不借此
缩减其余 FR/章节范围，完整 PRD 目标继续开放。

代码冻结后的验证：

- 后端完整回归 **1715 passed / 13 skipped**，76.15 秒；相对基线新增45项测试。
  跳过8项真实Provider及5项独立Chromium环境门控测试。
- 前端 **300 passed / 31 files**；新增8项恢复预览/确认/迟到响应/网络结果查询与分页测试。
- 评测工具 **59 passed**；离线 Snapshot 门禁 **61/61**。
- Ruff、ESLint、TypeScript、Vite build 与 diff-check 通过。
  主 JS 697.32 kB，既有大于500 kB提示继续保留。

## 2026-09-09：Deep Research 资源面板与真实原生研究验收

交付前基线：`ce91687`。合同见 [Deep Research 运行资源](deep-research-resources.md)。
本批代码、测试、真实验收归档与本节同步交付。

中英文工作区展示已固定的共享 SDK 额度、已报告费用、剩余时间、原始截止时间、
轮数政策、实际原生研究单元与任务累计快照。没有报告的用量为空，进度快照不重复
相加；Run 结束不补造任务成功。API 有界回放、身份/序号检查、读前后权限复验、
no-store；前端串行刷新、单调时钟倒计时、取消整次研究、旧请求作废与失败清空。

研究单元只允许三种已配置类型，每种每个逻辑 Run 新建一次；续接必须是本 Run 已
观测到的原生 agent_id。三份来源包由主 Agent 按原始来源去重、保留冲突、复核原件，
经既有 receipt/文件验证与 record_evidence_batch 落账。来源包是输出约定，不能
自行升级为可信凭证。任何模式下子代理再派生也在钩子和回调中明确拒绝。

真实 SDK 首次执行发现 `dontAsk` 与启动配置给只读工具添加的 ask 规则冲突，导致
三个单元均启动却无法读取来源。失败归档保留在
[`research-e2e-20260910T022515988906Z`](../evals/reports/research-e2e-20260910T022515988906Z/report.md)，
未将主 Agent 独自完成报告当成研究验收通过。修复仅移除 Deep Research 明确只读
工具的重复 ask，保持统一启动钩子、身份/路径验证和主 Agent 写入权限。

修复后的真实运行见
[`research-e2e-20260910T023412298562Z`](../evals/reports/research-e2e-20260910T023412298562Z/report.md)：

- 固定 SDK 0.2.143，主消息服务模型 `deepseek-v4-pro`；原生子任务返回的 resolvedModel
  为 `deepseek-v4-flash`，不将该配置/解析字段冒充独立计费认证。
- 三种研究单元全部实际启动、执行时间重叠、分别完成三次原件 Read，返回非空
  来源包；无子代理写入、Bash、外部搜索或业务外发。
- 主 Agent 再次读取三份原件，两份报价日期/价格/MOQ差异保留；法语转载不另建
  证据。批量提交的重复项返回同一 ID；决策/不可变报告关联真实证据与决策 ID。
- 16,520 条实际事件完整回放；运行中资源采样和最终三个任务快照与原生最终通知
  的 token/工具数/时长逐项相符；报告字节校验和与不可变产物一致。
- Run 正常完成，SDK 累计估算 **1.920009 USD**，固定上限2 USD，约180秒内结束，
  原始墙钟600秒、轮数不限。首次失败运行估算1.849989 USD；均不是厂商实际账单。

复验原始 `summary.json` 唯一误报来自验收器漏列原生只读 Glob 文件定位，原判定
保持不变。补齐 Glob/Grep 并保留外部工具拒绝的回归后，对同一份不可变归档重算，
[`reverification.json`](../evals/reports/research-e2e-20260910T023412298562Z/reverification.json)
全部通过，没有第三次模型调用。旧失败归档在同样规则下仍失败。事件以无损 gzip
保存，压缩前后字节数及 SHA-256 均可复核。

冻结代码后的验证：后端 **1771 passed / 13 skipped**（77.08秒），前端
**308 passed / 32 files**；Ruff、ESLint、TypeScript、Vite build、diff-check通过；
评估工具 **59 passed**、Snapshot **61/61**。主 JS 706.99 kB，既有体积警告保留。
Browser 连接仍返回无可用浏览器，发现列表为空，未声称目视验收通过。

本批证据只覆盖合成上传材料上的真实原生执行与来源合并；开放互联网研究质量、
生产资源/故障矩阵、组织治理、其余 FR/章节 deferred 均保留，完整 PRD 目标继续开放。

## 2026-09-09：共享项目成员界面与持久团队讨论

交付前基线：`d801e89`。合同见 [项目协作](project-collaboration.md)。

- 中英文“团队与评论”面板显示当前账号和项目能力；所有者/管理员可以核对账号、
  角色和当前 revision 后添加、改角色或移除成员。保留现有唯一所有者、租户与
  签名角色一致性约束；本地模式明确说明仅本机评论，不模拟组织账号或发送邀请。
- 评论可绑定整个项目、当前 Run、主 Agent 完整可见消息、证据、决策或准确文件
  版本。回复继承原关联；作者可编辑，作者/管理员可移除或解决/重开，查看者只读。
  原文及所有修改保持历史，移除后的原文仅作者/管理员可查看。
- 项目外独立存储保留合法连续历史、序号、身份与标签快照；原子落盘、精确字节
  容量、请求幂等、revision 冲突保护、跨进程成员租约、提交前后权限复验。
  真实双进程提交/编辑/撤销竞争及保存后进程退出重试均已通过；冷备往返保留讨论。
- 界面每15秒刷新，旧回复草稿保持挂载；请求失败保留草稿，冲突时先核对最新内容。
  切换项目、撤销/过期和关闭面板使旧请求作废；权限拒绝后迟到的能力响应不能恢复
  私有内容。协作所有响应禁止缓存；账号中的 @/+/冒号保持网关要求的规范路径。
- 修复回归中确认的首次点击早于 effect 初始化而不发起历史读取的问题；凭据在
  请求等待期间失效也返回访问拒绝，不泄露诊断文本或留下可继续操作的私有界面。

最终验证：后端完整 **1790 passed / 13 skipped**（81.82秒）；增强规范账号路径的
同批18项协作测试再通过。前端完整 **323 passed / 33 files**；评测工具 **59 passed**，
离线 Snapshot **61/61**。Ruff、ESLint、TypeScript、Vite build、diff-check通过。
主 JS 732.88 kB，既有大于500 kB的体积提示保留。

Browser 发现列表仍为空，未声称真实浏览器目视通过。项目模板与组织私有 Skill
仓库尚未实现；真实组织身份联调、大容量/长期运行及生产隔离仍需验收。此次只更新
成员协作和评论的已交付边界，其余 FR/章节 deferred 与完整 PRD 目标继续开放。

## 2026-09-09：可复用项目模板与创建恢复

交付前基线：`e68481f`。合同见 [项目模板](project-templates.md)。

- 中英文工作台可核对并保存项目名称、目标、简介、市场、约束及选定文件版本，
  查看不可变版本和 SHA-256、下载准确参考副本、保存新版本、归档与恢复。
- 所有者可修改模板起始内容、明确选择参考文件并核对后创建独立项目；保留模板
  来源和文件版本映射，创建新的项目内文件 ID，PDF/图片沿用 Markdown 摄取。
  新项目独立建立证据、决策、成员及定时任务，模板创建不启动 Run 或模型调用。
- 作者/管理员管理模板，成员可整理、查看者只读；新建项目保持现有所有者门槛。
  模板与创建记录独立于 Agent 工作区，持有跨进程权限租约，发布前复验身份。
  结构化文本先脱敏；只复制核对过的原文件字节；请求、文件与历史均有容量限制。
- 创建记录先落盘，私有暂存目录形成完整项目后排他原子发布。相同请求只创建一次，
  响应丢失后的重试保留新项目后续编辑。重开浏览器可查看持久创建记录，核对后恢复，
  无须原浏览器草稿或旧会话。真实双进程竞争及发布前/后进程突然退出均已验证。
- 冷备份验证模板、固定文件、已完成创建与目标来源并可恢复到新目录；未完成创建
  或暂存目录必须先在原安装中完成恢复。修改或软链接参考文件不会发布半成品。
- 独立服务进程的 [HTTP 演练](../backend/scripts/check_project_templates.py) 已通过：
  临时目录中上传、两个不可变版本、参考下载、创建与重试、归档限制，最终两个项目、
  一个复制参考、零 Run；该结果不作为模型或真实组织身份联调的证明。

完整回归：后端 **1809 passed / 13 skipped**（86.77秒）；抽取共享协作身份常量后，
20项评论与模板 API 复验通过。前端 **331 passed / 34 files**；评测工具 **59 passed**，
离线 Snapshot **61/61**。Ruff、ESLint、TypeScript、Vite build、diff-check通过。
主 JS 754.37 kB，既有大于500 kB的体积提示保留。

Browser 发现列表仍为空，浏览器目视验收保持开放。组织私有 Skill 仓库、真实组织
身份联调、大容量/长期运行、生产隔离以及其余 FR/章节 deferred 尚未完成；完整 PRD
目标继续开放。

## 2026-09-09：组织私有 Skill 仓库

交付前基线：`f4e5102`。合同见 [私有 Skill 仓库](private-skill-repository.md)。

- 中英文 Skills 页面提供组织私有包检索、固定版本文件预览、原始 ZIP 导出和管理员
  导入审核。ZIP 上传只形成待审副本；审核确认后发布并保持禁用，启用及恢复独立操作。
  人类上传不创建 Run，也不伪造 Agent 提案的项目/运行来源。
- 扫描失败不能强制发布。审核绑定原始包哈希、导入修订、操作者和当前版本选择代数；
  相同请求幂等重试，并发修改要求重新核对。Installed 页也校验显示的私有包状态后
  提交版本比较请求，避免旧卡片修改新版本。
- 使用现有 Registry 不可变历史与原子发布凭证。真实双进程重复审核仅留一个决议；
  发布前、保存历史包后、提交凭证后的中断均保持可核对结果。新导入尚无提交凭证的
  历史包不向成员展示/下载，不能经旧 rollback 接口选中。原有已发布历史继续可读。
- 单部署组织范围持久绑定；管理员写入、成员只读已发布包，提交前再次检查断言有效期。
  私有管理草稿在身份或角色变化后卸载，迟到响应不恢复旧状态。保留原始下载字节，
  文本预览脱敏且有长度限制；包、导入记录与审核历史均有上限。
- 冷备份验证导入身份、冻结文件、元数据、审核连续性及共享发布凭证，并能恢复
  pending/rejected/published。未核对的 publishing 和上传暂存先阻止备份，须在
  原安装由管理员刷新审核记录完成核对，再停机制作快照。
- 独立服务进程的 [本地 HTTP 演练](../backend/scripts/check_skill_repository.py) 通过：
  两个审核导入、两个固定版本、上传/发布重复请求、独立启用、历史恢复、过期选择拒绝
  和准确原包下载；临时数据退出后删除，零模型 Run。

完整回归：后端 **1835 passed / 13 skipped**（104.19秒）；前端 **345 passed / 35 files**；
评测工具 **59 passed**、离线 Snapshot **61/61**。Ruff、ESLint、TypeScript、Vite build、
diff-check 通过。主 JS 775.32 kB，既有大于 500 kB 的体积提示保留。

Browser 发现列表仍为空，浏览器目视验收保持开放。真实组织身份联调、跨组织物理隔离、
长期容量/生产恢复、真实模型使用质量及其余 FR/章节 deferred 尚未完成；完整 PRD
目标继续开放。

## 2026-09-10：Shopify 店铺只读连接器与店铺体检 Skill

交付前基线：`469b3e1`。合同见 [电商平台接入合同](marketplace-connectors.md)。

- 用户要求接入 Shopify 店铺，并确认参照 Accio Work 插件分层：宿主 Connector 持有凭据
  并执行读取，Plugin 只放 Skill。Accio `shopify-plugin` 仅核对清单与 Skill frontmatter
  （`mcpServers` 为空、宿主 OAuth 连接器、捆绑 Shopify CLI、4 个固定角色子代理），未复制
  Skill 正文；固定角色子代理与沙箱内 CLI 持有令牌不符合红线，未采用。
- 2026-01-01 起 Shopify 后台不能再新建自定义应用。连接器保存店铺地址与 Dev Dashboard
  应用 Client ID/secret，服务端经 client credentials grant 换取 24 小时令牌，只存进程内存，
  到期前 60 秒刷新、HTTP 401 后丢弃，不落盘、不返回。
- 固定 GraphQL Admin `2026-07` 文档读取店铺、商品（ACTIVE/DRAFT/ARCHIVED/UNLISTED）
  与订单；订单不请求顾客姓名、邮箱、电话和地址，未授予 `read_all_orders` 时标注最近
  60 天窗口。连接器允许与 Shopify 实际授予的权限双重校验；商品/订单查询核对 shop ID，
  店铺域名不符即拒绝。GraphQL 200 错误与 HTTP 401/402/403/404/423/429 映射固定错误码，
  任何错误整页拒绝。
- 与 TikTok Shop 共用固定地址 DNS、TLS/SNI、无重定向、2 MiB 与 30 秒边界（抽出共享
  传输函数）；`MarketplaceReader` 按连接器类型分派，管理员读取路由、项目店铺授权与主
  Agent `read_marketplace` 两平台共用。新增 `operation=orders`，订单页签发
  `marketplace_order_page` 来源凭证；不新增 MCP 工具或 server。
- 连接器页面新增 Shopify 卡片：店铺地址规范化、凭据保存、读取店铺/商品/近期订单、
  缺失权限与多余写权限提示、项目授权；中英文文案齐全。
- 新增 `ophira-core:shopify-store-review` Skill（数据边界参考与体检模板），主 Agent 只经
  已授权读取做店铺体检；Registry 审核 5 项通过，应用 Skill 由 13 个增至 14 个。

验证命令与结果：

- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1885 passed / 13 skipped**（99.31秒），其中新增 Shopify 测试 49 项。
- 前端 `npm run test:run`：**351 passed / 36 files**（新增 Shopify 卡片测试 6 项）；ESLint、
  Vite build 通过，主 JS 786.26 kB，既有大于 500 kB 提示保留。
- 本批改动文件 Ruff 通过；全量 Ruff 仍报 9 项，均在非本批的未跟踪 `project_export_*.py`。

本批未使用真实 Shopify 凭据，也未发送真实 Shopify Admin API 请求；目标店铺
`skateboard-shop-7560.myshopify.com` 仅核对过公开 `meta.json`。真实店铺联调（需运营者
创建 Dev Dashboard 应用并在界面保存凭据）、组织外店铺授权码流程、旧版后台令牌、商品/
订单写入、Shop browser 平台列表加入 Shopify 及其余 FR deferred 继续开放；完整 PRD
目标继续开放。

## 2026-09-11：工作台授权模式与显式 Deep Research 入口

交付前基线：`3a58fc4`。本批只动前端工作台输入框，未改后端契约。

- 输入框工具菜单收敛为「添加文件 / 专家 / Skills / 连接器」，Skills 与连接器直接进入已有管理页。
  删除只改写提示词的内置 Skill 目录（`skillCatalog.ts`），它与运行时真实的 Plugin Skill 发现重复，
  且会让用户以为选中的条目参与了装配。
- 新增授权模式选择器：读写同一份 `/api/org/policy` 的 `permission_mode`，只提供后端
  `PermissionModePolicy` 实际存在的 `default` 与 `auto`，不提供任何越过沙箱的完全放行档；
  保存前先取最新策略再写回，失败保留菜单与原因，任务运行或等待授权时禁用。
- Deep Research 保持 PRD §3.5 场景 G 的「用户显式选择」：改为输入框内的独立开关
  （`aria-pressed`，默认关闭，运行中锁定），选择随 `mode` 提交到 `POST /api/projects/{id}/runs`。
  **此前未提交的改动只删掉了模式菜单、没有替代入口，界面上已无法发起 Deep Research；本批补回该入口**，
  FR-19 的运行时能力本身未变。
- 授权与 Deep Research 药丸在停靠态和 430px 窄屏收敛为图标并保留无障碍名称；停靠态输入框由四列
  改为五列，顺序为 工具 / 授权 / Deep Research / 模型 / 发送。

验证命令与结果：

- 前端 `npm run test:run`：**357 passed / 36 files**（新增 Deep Research 开关与授权模式用例）；
  `npm run lint`、`npm run build`（含 `tsc -b`）通过，主 JS 783.24 kB，既有 >500 kB 提示保留。
- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1885 passed / 13 skipped**（93.55秒），与上一批基线一致；本批未改后端。全量 Ruff 仍报 9 项，
  全部在未跟踪、未纳入本批的 `project_export_*.py`。
- 真实浏览器复核（仓库 `.venv` 的 Playwright + Chromium，临时 `OPHIRA_DATA_DIR`，真实后端进程）：
  入口态与停靠态各一轮，**发现列表为空**。校验点：Deep Research 默认关闭且开关往返；run 请求体
  `mode=deep_research`（该请求被拦截并返回 503，未真正启动 Run、未调用模型）；授权菜单恰好两档，
  选择后 `/api/org/policy` 变为 `auto`，刷新后仍显示已保存档位；停靠态五个控件同一行且顺序正确、
  不溢出；400px 窄屏控件仍可见可点。检查结束把组织策略复位为 `default`。
- 复核中修正：按下态背景被 `:hover` 规则以更高优先级盖掉，选中态看起来与悬停一致，
  已给 `[aria-pressed='true']` 补 hover 变体。

边界：未调用真实模型、未启动 Run、未使用真实 Provider 凭据；Deep Research 的真实研究质量、
真实取消/耗尽与长时墙钟故障矩阵仍 deferred。观察到一个既有问题、本批未改：400px 窄屏下固定输入框
与页面文字重叠，属于原有停靠层布局，未纳入本批范围。完整 PRD 目标继续开放。



## 2026-09-22：产物与记忆观察事件

本批最初基于本地 `eff2289` 实现 §4.12（提交 `ad00de7`）；核对远端后合入
`d7a5f9a` 及其已有文件历史、恢复、资源面板、协作和模板能力，合同见
[项目事件](project-events.md#artifact-versions-and-memory-proposals-2026-09-22)。

- 新增产物版本更新与记忆提案的持久、幂等事件；原生产构造、原生 MCP、媒体、
  浏览器和 Run 收尾/启动恢复/SSE 尾部共用 projector。没有新增模型 runtime。
- 原生记忆提案由服务器绑定来源 Run；事件只携带记忆标识，审阅内容另走授权查询。
  按 Run 打开的审阅面板支持观察后刷新、审批、修改、错误重试和中文文案；
  不自动批准，也不从事件正文伪造记忆。
- 合并时保留远端 Artifact 不可变快照、路径捕获和跨进程锁实现；
  重复最新内容不写新版本，A→B→A 保留三个版本；新增用例继续验证这条不变量。
  旧 artifact.registered 日志保持原样，匹配的旧通知被识别，不一致日志拒绝完成。
- 新增离线验收用例覆盖提交后退出、双进程补发/登记、来源范围、撤销、旧日志、
  原生 MCP、蒸馏、SSE 尾部、页面切换与审批/刷新竞争。

合并远端前的验证（仅证明 `ad00de7`，合并后另行复验）：

- 前端最终完整测试 **281 passed**（27文件）；lint、TypeScript/构建通过。
  主 JS 为653.86kB，既有大于500kB构建提示保留。
- `uvx --offline ruff check backend/src backend/tests`：通过；Python 编译与
  `git diff --check`：通过。
- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1563 passed / 8 live-only skipped**（83.73s），含新增24项验收。
- 新增授权用例发现并修复既有 Run 身份映射错误：记忆提案/蒸馏现在读取持久的
  `principal_role/principal_session_id`，不再读取只存在于请求身份对象上的字段。
  同时覆盖匿名/登录、项目/Agent 记忆与成员撤销。
- eval tools **59 passed**；Snapshot **61/61**；本轮没有调用真实 Provider 或外发服务。
- Python 3.11 环境及固定版本 SDK 0.2.143 依赖已安装完成；首次安装元数据超时，
  随后按 `backend/pyproject.toml` 的 dev 依赖完成安装，测试使用仓库 `PYTHONPATH`。
- Browser 连接返回 `No browser is available`，按技能故障发现流程确认可用列表
  为 `[]`；没有页面目视验收证据。

合并 `d7a5f9a` 后的完整复验：后端 **1909 passed / 13 skipped**（101.86s），
跳过项为 8 个需要真实 Provider 配置的合约用例和 5 个显式启用的 Chromium 用例；
前端 **369 passed / 37 files**，lint、TypeScript 与 build 通过，主 JS 784.84kB，
既有体积提示保留。全量 Ruff、eval tools **59 passed**、Snapshot **61/61** 与
`git diff --check` 通过。合并保留远端已有能力，没有把历史验收冒充本轮执行。

本批不增加真实 Provider 提案质量或生产隔离证明；无 Run 项目事件及
schedule/preflight 细分事件继续 deferred。已有 Bash 工作区检查点与资源面板
以其独立合同和证据为准，不因本批历史基线较旧而回退；全量 PRD 目标保持开放。

## 2026-09-22：Office 原件参考检索

交付前基线：`aaf0dff`，该基线已正常推送至远端 main。
新增交付合同：[Office 项目参考资料](project-knowledge.md#office-references-2026-09-22)。

- 已登记 DOCX 正文/表格、XLSX 实际单元格、PPTX 文本/表格/组合形状/演讲者备注
  可经现有项目知识库 API、只读原生 MCP 和中英文面板检索；没有第二份持久索引。
- 结果携带段落、工作表/单元格、幻灯片/备注位置；摘录限于对应块，较短单元格保留
  公式及缓存值说明。行列标明属于提取文本，不伪造 Office 文件或渲染页面坐标。
- 保留当前注册版本、校验和、权限复验和原件下载。公式/宏/外部引用不执行或取回，
  缓存值明确可能过期；隐藏内容、未覆盖内容及格式限制在检索结果和空查询覆盖面中显示。
- Office 解码先验证压缩包和全部 XML：限制展开体积、节点、成员、文本和 Word 表格
  展开量，拒绝 DTD（含 UTF-16）、重复/越界路径、加密包和歧义定位；跨文件预算在
  解码失败后仍计费。不会把部分失败结果称为完整索引。`lxml` 已是原有 Office 库的
  安装依赖，本批声明其直接依赖范围；SDK 仍固定 0.2.143。

验证（最终代码）：

- 后端完整回归：**1941 passed / 13 skipped**（101.20s），较基线新增32项。
  13项仍为8项真实 Provider 合约和5项需显式启用的 Chromium 验收。
- 前端完整回归：**374 passed / 37 files**；lint、TypeScript 与 build 通过。
  主 JS 787.30kB，既有体积提示保留。
- 全量 Ruff 和 `git diff --check` 通过。本轮未改评测执行器；合并基线的
  eval tools 59 passed、Snapshot 61/61 记录见上一节。
- 用真实应用导出的 Office 包、授权 HTTP 下载及原生 MCP 做本地验证；另覆盖
  表格/链接、稀疏及共享字符串、公式缓存、幻灯片重排/组合/备注、版本替换和攻击输入。
  没有真实 Provider 调用、联网公式执行、文档渲染或浏览器目视验收。

仍 deferred：旧版 DOC/XLS/PPT、加密/Strict 变体、图表图片及复杂嵌入内容的语义，
真实检索质量、容量、进程级解码隔离与版式/浏览器验收。全 PRD 目标继续开放。

## 2026-09-22 Provider / Connector 凭据加密导出与确认删除

交付前基线：`0f0deac`。操作与恢复合同见 [凭据管理](credential-management.md)。

- 设置页新增中英文凭据备份与删除入口：只展示可用状态，单凭据密码加密下载，
  删除须显式勾选确认；请求绑定所审阅版本，失败后刷新状态，关闭后不处理迟到下载。
  删除成功同步更新 Provider 的密钥状态。
- 管理接口只允许本机操作者或当前部署租户的 owner/admin；版本标识用私有 HMAC
  生成，不暴露密钥摘要。导出后复验身份有效期和版本，删除在事务内复验权限，
  重复请求返回持久凭证，旧请求不能误删后来保存的新密钥。
- 导出采用固定参数 Scrypt 与 AES-256-GCM，无明文临时导出；离线解密命令只生成
  新的 `0600` 文件，不打印密钥、不连接服务、不自动回写配置。
- 旧 Provider/Connector 密钥库按进程锁迁移；原子删除保留无密钥的撤销记录，阻止
  重启时环境变量回填。搜索、媒体、店铺与商业 adapter 共用删除感知的凭据读取；
  旧无 Profile 的 SDK 入口也检查环境凭据撤销。保存新凭据可显式恢复访问。
- 支持管理员配置的数据根目录别名，同时拒绝 vault 内链接/特殊文件与损坏内容。
  离线性能工具更新密钥读取保护以适配新格式，仍禁止读取既存 vault。

验证（最终功能代码）：

- 后端完整回归 **1982 passed / 13 skipped**（103.56s），新增41项；跳过项仍为
  8项真实 Provider 合约及5项需显式启用的 Chromium 验收。
- 前端完整回归 **384 passed / 38 files**；ESLint、TypeScript、Vite build、全量
  Ruff 与 diff-check 通过。主 JS 795.38kB，既有体积提示保留。
- 评测工具 **59 passed**，离线 Snapshot **61/61**。覆盖真实子进程竞争写入和
  提交前后退出、重启禁用、加密篡改、离线解密、过期身份、旧版本请求、HTTP权限及
  原生搜索/媒体 MCP 删除后无网络请求；没有调用真实模型、商家或外发服务。
- Browser发现列表仍为空；未宣称页面目视验收。删除没有操作任何真实用户密钥。

边界：删除不等于服务商端撤销，不擦除旧备份或已经加载密钥的工作进程，也不是
静态存储加密/物理擦除/多租户密钥隔离。项目与会话的授权导出/删除、其他服务凭据、
全应用恢复UI、真实身份联调与生产灾备等 deferred 继续保留；全 PRD 尚未完成。

## 2026-09-22 项目与应用会话授权导出

交付前基线：`2da33b4`。范围与 API 合同见 [项目数据导出](project-data-export.md)。

- 工作区新增中英文导出入口，分别核对整个项目或当前会话，再下载绑定该次
  数据指纹的 ZIP；重复点击、关闭、切换项目/Run 与迟到下载均受控。
- 项目归档包含工作文件、不可变产物版本、检查点、Run 账本、项目关联业务记录、
  模板与参考文件、讨论、计划、Skill 候选及项目记忆当前值/历史/删除记录。
  会话归档按同项目 SDK 会话/续接关系选择 Run，只带相关产物与来源记忆；
  可读文本保留主线/支线区别和未完成文本，原始事件保留完整顺序与来源。
- 每个文件有大小及 SHA-256 清单。旧工作区历史字节缺失明确列警告，
  不可变快照缺失则拒绝导出；已删除记忆正文不因旧历史残留重新出现。
- 当前 owner/admin 权限贯穿预览、捕获及下载分块；执行租约阻止运行中捕获，
  创建前/写入时/创建后的全清单复验阻止过期核对与混合版本。拒绝链接、特殊
  文件、路径别名与越界容量，断连和异常关闭私有临时归档。
- 不导出凭据库、SDK 内部日志/配置、浏览器登录态、设备密钥、共享 Agent 核心
  或其他项目。ZIP 未加密且不是应用恢复格式，UI 和文档明确这些边界。

验证（最终功能代码）：

- 后端完整回归 **2021 passed / 13 skipped**（104.39s），新增39项；跳过项仍为
  8项真实 Provider 合约及5项需显式启用的 Chromium 验收。
- 前端完整回归 **393 passed / 39 files**；ESLint、TypeScript、Vite build、
  全量 Ruff 与 diff-check 通过。主 JS 802.33kB，既有体积提示保留。
- 评测工具 **59 passed**，离线 Snapshot **61/61**。验证授权 HTTP ZIP 和清单、
  同会话续接、交错支线流、权限到期/撤销、捕获期间新增数据、损坏事件、
  不安全路径、历史版本缺失、记忆删除残留及流式下载断连。
- Browser 发现列表仍为 `[]`，没有页面目视验收；未调用真实模型或商家服务，
  未导出或删除真实用户项目数据。

仍 deferred：项目/会话删除、原生 SDK 完整会话可迁移性、其他服务凭据、
全应用恢复 UI 与生产灾备；这些不由本轮 ZIP 导出替代。全 PRD 目标继续开放。

## 2026-09-22 删除事务与应用写入互斥基础

交付前基线：`f52f8dd`。实施范围与剩余工作见 [项目数据删除](project-data-deletion.md)。
**本批尚未提供项目/会话删除 API 或确认界面，不把数据删除功能标为完整交付。**

- 精确文件计划在授权和字节/目录完整性复验后原子提交，删除及共享记录替换有
  持久恢复清单；只暂存保留字节，完成凭证不含正文或文件路径清单。容量、
  符号/硬链接、特殊文件、路径别名、源变更和损坏事务均拒绝扩大操作范围。
- API 身份 nonce 写入、完整下载流、独立 Run、后台计划/意图/渠道/Push、
  上传等线程及店铺浏览器宿主参与数据操作租约。取消请求不能提前释放仍在
  写入的线程；各独立任务持有自己的描述符，不继承已结束请求的锁状态。
- 已提交而未完成的删除关闭普通数据访问；新进程在初始化业务存储前继续清理，
  不争抢活进程的独占操作。冷备份在复制前拒绝未完成的删除，并与提交/恢复互斥。
- 当前互斥粒度为单部署数据目录，不强停活 Run、流或浏览器；调用方还必须完成
  项目/会话归属、共享记录、SDK 清理、最小凭证和禁止恢复/重建的业务模型。
  固定 SDK 的原生 `delete_session` 接口已核对，尚未在本批调用。

新增本地验收覆盖真实子进程在提交、unlink、替换、写凭证和完成改名后退出，再由
新实例恢复；另有授权撤销、保留数据、损坏记录、配额、真实 ASGI 流、独立任务、
取消后上传线程、后台服务/浏览器宿主门禁、跨域忙状态、启动恢复和冷备份拒绝。
没有删除真实用户项目、调用真实模型或商家服务；没有前端修改或页面目视验收。

项目/会话删除的专用范围、确认 API/UI、SDK 清理、共享引用、持久禁止重建、
完整业务端到端和生产验收继续 deferred。全 PRD 目标继续开放。

验证（最终代码）：

- 后端完整回归 **2066 passed / 13 skipped**（105.82s），较基线新增45项。
  跳过项仍为8项真实 Provider 合约与5项需显式启用的 Chromium 验收。
- 全量 Ruff、`git diff --check`、评测工具 **59 passed**、离线 Snapshot
  **61/61** 通过；本批没有前端代码变更，没有重跑或冒称新的页面验收。
- 完整回归发现了新增中间件顺序影响本地来源校验的问题，已恢复原有来源检查
  顺序；维护忙响应另经同一 CORS 实现处理，忙时预检请求仍拒绝。相关来源、
  身份与互斥测试41项通过，最终完整回归也通过。
- 另一次完整回归中，既有凭据并发测试的子进程曾在打开临时目录内锁文件时
  收到一次 `ENOENT`，根因未定。相同用例随后连续10次单独通过，凭据模块18项
  通过，最终完整回归通过；未以推测修改凭据实现，此异常保留在验证记录中。


## 2026-09-22：会话删除的业务闭环（基于 79e6972）

新增当前项目所有者/管理员的会话范围核对及严格确认 API、中英文工作区入口。
范围沿 SDK session 和双向 resume 来源覆盖关联 Run，核对指纹绑定当前文件与
项目版本；提交前复验授权，在独占操作中调用固定 SDK 0.2.143 的公开会话删除。
SDK 子进程不继承宿主凭据；精确事务继续清理支线、会话辅助文件和共享历史，
未归属孤儿、链接、重复会话、活动任务及中断写入拒绝提交。

清理事件/摘要/运行上下文、来源记忆历史及 Agent 镜像、日记、manifest、通知/
反馈/授权/投递和相关经验条目；讨论/Skill 来源摘要去正文，独立保存成果保留。
仅保留内容为空的 Run 来源，不能读取/续接/重试或同 ID 重建；工作区版本、
项目导出、候选来源校验与冷备往返仍然有效。前端明确勾选、断线同请求重试，
成功后清理会话显示与旧 SSE，迟到结果不会清掉新选择的项目。

完整范围、保留项和恢复限制见 `docs/project-data-deletion.md`。其他会话的历史
上下文和独立副本、保存文件、人工讨论、模板/Skill、商业审计、自动化书签、
凭据/浏览器登录、旧备份、外部服务、未归属缓存及 OS 临时文件明确不在单会话
删除范围内；不是底层介质擦除，也不是整个项目删除。本批未删除真实用户数据，
未调用付费模型或真实商家服务。Browser 连接选择返回不可用，发现列表为空，
没有进行或冒称页面目视验收。

验证（最终代码）：

- 后端完整回归 **2084 passed / 13 skipped**（112.34s），新增18项；跳过项仍为
  8项真实 Provider 合约及5项需显式启用的 Chromium 验收。
- 前端完整回归 **398 passed / 40 files**，新增5项；ESLint、TypeScript、Vite build
  通过。主 JS 809.25kB，既有 bundle-size 提示仍在。
- 全量 Ruff、`git diff --check`、评测工具 **59 passed**、离线 Snapshot **61/61** 通过。
- 单会话业务用例17项，另加 schema 1 指纹兼容 / schema 2 新建删除标记的 CAS
  测试；完整回归同时覆盖已有跨进程崩溃与 HTTP/后台互斥用例。

整个项目删除、会话迁移、其余服务凭据、生产恢复与上表所有 deferred 保持开放；
完整 PRD 目标尚未完成。

## 2026-09-22：整个项目删除的业务闭环（基于 dc20679）

实施合同：[项目与会话删除](project-data-deletion.md)。本批把项目删除接入当前
owner/admin 授权、人审范围指纹、准确名称确认、持久事务恢复和中英文工作区入口。

- 清理完整项目工作区/文件版本/检查点、全部 Run、项目记忆和可归属的共享上下文、
  协作/模板/Skill 候选、来源/商业记录、计划/意图、店铺浏览器登录态及项目访问授权。
  共享设备只移除本项目范围；无剩余范围的配对与 Push 订阅一起删除。
- 固定 SDK 0.2.143 公开会话列表发现尚未落入 Run 的原生会话；验证实际 cwd 后
  清理对应项目目录、附属数据、历史和配置项，兼容工作区缺失和已删除的会话。
  不调用模型，不根据推测的路径扩大删除范围。
- 无正文的 `ErasedProject`/`ErasedRun` 保留 ID，阻止创建、续接和旧模板请求复活。
  独立模板副本保留原件与设置，源删除后以摘要证明校验来源和冷备；目标删除会
  清理上游创建请求及对应摘要。已发布共享 Skill、全局凭据、其他项目保留。
- 事务提交前再次验证权限；授权过期/撤销、活动 Run、源变更、确认名错误和链接
  拒绝删除。项目评论/店铺授权的原子写入残留仅按精确目标归属清理。
- 前端必须核对、输入项目名并勾选确认；断线重试保留原请求，关闭面板不被描述为
  取消。成功后清除所删项目与会话状态；迟到列表、刷新、SSE 和结果不污染新项目。

验证（全部为临时合成数据，没有删除真实项目或调用外部服务）：

- 后端全量 **2100 passed / 13 skipped**，117.35秒。13项仍为8个真实 Provider
  合约与5个需显式启用的 Chromium 验收；没有将跳过用例计为通过。
- 新增项目删除业务 **16 passed**，涵盖 SDK 原生数据、范围隔离、模板源/目标
  删除顺序、冷备恢复、共享设备、已删会话、权限复验、部分清理恢复和 HTTP 重试。
- 前端 **406 passed / 41 files**；lint、TypeScript 与 build 通过。主 JS 为
  816.57kB，既有500kB分包警告仍保留；Ruff通过，eval工具 **59 passed**，Snapshot **61/61**。
- Browser连接返回 `No browser is available`，恢复流程查询列表为 `[]`；未进行页面
  目视验收，不使用自动化组件测试代替此项。

仍保留：无法归属的运行/系统缓存、已有备份和外部服务数据、物理擦除边界；原生
完整会话迁移、其他服务凭据、全应用恢复 UI、生产资源/租户隔离、真实服务/人工
质量验收，以及总表的其他 deferred。完整 PRD 目标仍未完成。

## 2026-09-22：完整应用备份与恢复的本机维护界面（基于 6f41d29）

实施合同：[应用冷备份与本机维护](application-backup.md)。设置页增加启动指引，
独立中英文维护页面复用已验证的停机快照，不构造业务应用或启动 SDK/自动化。

- 仅监听 loopback；随机私有会话密钥八小时到期，URL fragment 读取后清除，
  不持久存入浏览器。校验固定 Host、同源 Origin、授权头、受限正文与严格确认值。
- 备份和恢复均先生成私有完整副本，再展示核对时间、数量、提案状态与新目录。
  人工确认后复验代码指纹、目录身份和全量字节，再独占发布；不覆盖已有目录。
- 持久 `publishing` 记录是提交边界；两个真实进程退出窗口均能按原请求继续。
  未提交副本可丢弃；已提交副本只可恢复原发布。断线重试不产生第二份输出。
- 恢复仅生成新 data/Plugin 目录；界面明确完整副本包含凭据、登录态和自动化、
  本地目录未加密、原生会话依赖原 cwd。部署切换仍由本机操作者另行执行。
- 全量回归发现 Vault 首次并发创建锁文件的 ENOENT。独立标准库演练中，
  原方式60次打开失败30次，排他创建方式60次均通过；加强后的同步双进程用例
  在旧实现失败，修复后连续10次通过。保留 no-follow、私有普通文件与 flock 检查，
  新增锁软链接/FIFO拒绝验证。

验证：

- 后端全量 **2115 passed / 13 skipped**，119.02秒；其中凭据/应用备份/维护
  专项 **79 passed**，新维护用例 **13 passed**。跳过项仍为8个真实 Provider 合约
  和5个需显式启用的 Chromium 验收。
- 前端 **418 passed / 43 files**；lint、TypeScript、Vite build通过。构建新增维护
  入口及共享分包；Ruff、`git diff --check`、eval工具 **59 passed**、Snapshot **61/61**通过。
- [真实 HTTP 合成演练](../evals/reports/maintenance-console-20260922T211420980561Z/summary.json)
  绑定最终 Python 代码和编译资源哈希；157个文件、一个项目/Run、五份提案，
  删除合成源目录后恢复成功，canonical不变，凭据保留于磁盘而不出现在HTTP响应；
  损坏备份和错误授权/确认被拒绝，重复确认返回同一结果。
- 演练只访问 loopback 和临时合成目录，未读取正式数据、调用模型或外发。
  Browser仍无可用实例（列表为 `[]`），不宣称完成页面目视验收。

剩余：原生 SDK 完整会话跨路径迁移、其他服务凭据、部署切换、生产资源/租户隔离、
真实服务/人工质量与浏览器验收，以及总表其余 deferred。完整 PRD 目标仍未完成。

## 2026-09-22：Provider 选择时的数据区域与跨境提示（基于 a1468fb）

实施合同：[Provider 数据披露](provider-data-disclosure.md)。工作区模型列表、
选中后的输入区域、Provider 设置和编辑预览共用目的端点/区域说明。区域明确为
管理员填写，无法取得配置和 Auto 路由不猜测数据地点；地址仅显示 HTTP(S) origin，
不回显 URL 内的凭据、路径、query 或 fragment。提示模型上下文与工具自身的数据流，
未新增确认门槛、改变模型路由或声称实现区域强制。

新增组件7项、选择后保留提示的工作区集成1项，既有设置用例补充预览/保存验证。
前端全量 **426 passed / 44 files**，lint、TypeScript 与 build通过；后端无变更，
沿用本日 **2115 passed / 13 skipped** 基线。没有发起真实 Provider 调用。
Browser无可用实例，目视验收保持开放。真实区域、供应商条款、生产出口约束及
其他 deferred 继续保留；完整 PRD 目标仍未完成。

## 2026-09-22：飞书与 Web Push 服务凭据完整生命周期（基于 2828ebd）

实施合同：[凭据管理](credential-management.md#application-service-credentials)。
搜索、媒体凭据已有 Connector 管理；本批补齐飞书事件签名、飞书应用密钥和
Web Push 私钥的管理员加密导出、离线解密、确认删除、环境回退禁用和显式重配。

- 复用现有私有 Vault 与人审版本绑定；值和版本在同一事务读取，重配在持锁时
  再次核对当前权限。旧操作不能覆盖另一管理员的更新，也不能删掉重新保存的密钥。
- 飞书实时解析事件签名密钥；回复适配器按凭据版本失效缓存，等待中的旧 tenant-token
  响应不能恢复已撤销的授权。发出请求前复验，不以仍在内存中的旧令牌继续发送。
- Push 刷新 signer/公钥，删除或存储无法验证时停止新请求；已有配置的后台线程
  可在显式保存密钥后恢复。变更公钥要求设备重新订阅，不自动续接旧订阅。
- 中英文管理页展示各凭据的影响，支持掩码输入重配及版本冲突处理；响应只含元数据，
  断线后重新查询，关闭面板不被描述为撤销已经提交的操作。保存可能恢复待投递任务，
  在操作前明确提示，原有组织/项目/设备政策继续生效。

验证均使用合成凭据、隔离目录与模拟 HTTP 传输，未读取正式密钥或联系外部服务：

- 新增服务用例 **17 passed**：HTTP 授权、三种加密往返、删除/重配/陈旧版本、
  事务内权限过期、缓存令牌、等待响应时删除/轮换、现有进程观察另一进程删除、
  Webhook 轮换、Push 恢复与旧公钥订阅失效；源目录删除后冷备恢复仍保留密钥和删除状态。
- 后端全量 **2132 passed / 13 skipped**，124.69秒；13项仍为8个真实 Provider
  合约与5个需显式启用的 Chromium 验收。
- 前端全量 **429 passed / 44 files**；lint、TypeScript、build、Ruff、
  `git diff --check`通过；评测工具 **59 passed**、Snapshot **61/61**。
- Browser选择仍返回 `No browser is available`，恢复查询列表为 `[]`；未声称完成目视验收。

根身份网关签名和未使用的旧商业签名配置不授予普通服务管理 API；它们仍需部署所有者
在本机完成安全导出/删除/恢复的独立流程，防止组织管理角色获得签发身份的能力。
服务端撤销、旧备份/宿主环境副本、已经发出的请求、真实渠道验收和生产隔离，以及总表
所有其他 deferred 保持开放。完整 PRD 目标仍未完成。

## 2026-09-22：部署根凭据的本机导出、删除和恢复（基于 b489f56）

实施合同：[部署所有者凭据](deployment-credentials.md)。补齐身份网关签名密钥及
未被当前商业动作使用的旧签名配置；普通 HTTP owner/admin 不能取得这些根凭据。

- 独立本机命令只加载共用的根凭据配置，不构造应用或启动 SDK、调度、通知与交易。
  数据目录必须明确指定；可显式选择部署环境文件，无默认读取仓库配置的行为。
- 查看元数据、加密导出、确认删除、掩码重配及加密包直接恢复均绑定审核版本；
  恢复校验类型、凭据标识及租户，错误密码、旧版本和错误范围不能覆盖当前值。
  密钥和密码通过禁止回显的终端输入，不在命令参数或输出中传递。
- 改动使用应用停机独占锁，拒绝与当前版本 worker 或冷备并发；读取/导出持共享锁。
  删除持久禁止环境回退，重复删除可确认已有结果，旧删除不能影响重配后的密钥。
- 身份鉴权每次解析当前 Vault；删除、损坏和缺失拒绝新鉴权，轮换拒绝旧签名。
  显式 `local_vault` 配置支持不保留环境签名密钥的启动；绝不自动降级到 local_only。
- 完整冷备包含根 Vault 和删除状态；删掉原始 data/Plugin 后恢复仍保持保存值和撤销。
  已接受请求、外部网关、环境文件、旧导出包及旧备份仍是独立边界。

验证：新增根凭据 **16 passed**，包括真实独立 CLI 进程、HTTP 管理员隔离、停机锁、
加密往返、权限撤销、仅 Vault 启动、租户/密码/版本拒绝、源目录丢失后的恢复。
后端全量 **2148 passed / 13 skipped**，127.60秒；13项仍为8个真实 Provider 合约及
5个可选 Chromium 验收。首次全量因测试期间修改代码触发维护模块的
`implementation_changed` 保护；冻结代码后重新全量通过，没有放宽该保护。
Ruff、`git diff --check`、eval工具 **59 passed** 与 Snapshot **61/61**通过。
前端未变更，沿用本日 **429 passed / 44 files**、lint及build结果。

此批未修改正式密钥或联系真实网关。根密钥生产轮换/网关联调、外部副本删除、
生产租户与资源隔离、原生会话迁移，以及总表其他 deferred 仍需交付。
完整 PRD 目标仍未完成。

## 2026-09-22：SHEIN 店铺与 SPU/SKC 商品读取（4ebced0）

实施合同：[SHEIN seller reader](shein-connector.md)。已核验官方签名工具、店铺查询
3001499 和商品综合查询 3001634；记录公开页面脚本的来源与哈希，不依据第三方实现猜测。

- 凭据配置接受既有授权集成的 openKeyId 和已解密商家 secretKey，仅允许两个官方
  API origin。应用本地读取权限与平台实际权限分开，保存不等于访问验证成功。
- 每次商品读取先重新查询店铺并核对 supplierId；签名、固定请求路径、公开 DNS 固定、
  无重定向/自动重试、30秒截止和2MiB响应限制沿用现有安全边界。
- 商品按 SPU 分组分页，每页1–10组，保留 SKC 变体及英文标题、上/下架状态、
  类目、平台报告总组数和下一页；不混用组与变体数量，不虚构 SPU 标题或区域。
- 连接器配置和 Vault 凭据版本在请求前后复核；撤销、轮换、删后恢复同一密钥、
  权限缩减或存储损坏均拒绝旧响应。错误、日志和返回值不释放凭据或签名。
- 复用项目单店铺限时授权、主 Agent 原生 MCP 与完整观察内容绑定的来源凭证，
  不自动新增 Evidence。中英文界面支持配置、读店、筛选/分页、授权、撤销和重连。
  保存/撤销断线时先查询服务器现态，阻止重复提交和未核对的后续读取。

验证：

- 新增后端 **48 passed**，含独立 Node 算法签名向量、真实适配器的模拟 HTTP 合同、
  两次请求期间各类撤销/重配、原生 SDK MCP 调度与来源凭证、正式 FastAPI 权限路由。
  平台相关回归合计 **177 passed**。
- 冻结实现后后端全量 **2196 passed / 13 skipped**，128.72秒；跳过仍为8个真实
  Provider 合约及5个可选 Chromium 验收。前端全量 **441 passed / 45 files**，
  包含12项新增SHEIN交互用例；lint、build通过。
- Ruff、`git diff --check`、eval工具 **59 passed**、Snapshot **61/61**通过。
- Browser连接选择返回 `No browser is available`；按技能故障流程查询仍为 `[]`，
  未完成页面目视验收。没有使用真实卖家凭据、调用卖家接口或模型服务。

仍需交付：SHEIN真实卖家计划/账号与限流认证、授权交换/解密、SKU库存价格及订单，
Temu/AliExpress/Shopee适配器、商业写操作，以及总表其他功能和生产/人工验收项。
完整 PRD 目标仍未完成。

## 2026-09-22：SDK worker 资源限制与状态展示（08cf976 / 6203411）

实施说明：[SDK worker resource limits](sdk-worker-resources.md)。复用公开的 SDK
`cli_path` / `max_buffer_size`；不改 SDK 内部，不引入第二个模型或工具执行循环。

- 普通运行、Deep Research 和 Provider 预检均经字节透明的原生 CLI 监督进程；
  固定时长、总输出与帧限制，清理自身进程组，检测宿主退出，过滤无关环境凭据。
- Linux 必选模式通过 systemd user scope 限制 CPU、内存、交换与进程/线程数；
  执行 CLI 前核对实际内核限值，缺少环境或控制器即拒绝启动，不降级到 native。
  本机 macOS native 模式只证明时长/输出及自身进程组清理，不宣称 CPU/内存硬限制。
- Run 账本冻结原始限值/截止时间；跨审批续接累积输出。持久启动标记、丢失或损坏
  报告阻止额度重置；不把未知输出伪造成已观测消耗。启动配置与已核验状态分别展示。
- 普通运行新增可展开资源面板，中英文显示输出、Linux配置值、实际核验和异常状态；
  关闭即停止查询，Deep Research 保留共享预算与原生子任务展示。
- CI 增加 Ubuntu 24.04 专用资源验收 job，要求真实 CPU 限流、内存 OOM、进程数拒绝、
  脱离原会话的后代清理、管理器缺失拒绝，以及已钉版本的原生 CLI 启动测试。

本地验证：后端 **2220 passed / 20 skipped**，135.71秒；20项为8个真实 Provider、
5个可选 Chromium、7个仅Linux资源验收。前端 **445 passed / 45 files**，lint/build
通过；Ruff、diff检查、eval工具 **59 passed**、Snapshot **61/61**通过。
首次后端全量有8个旧 SDK 消息映射替身缺少新进程报告；补齐显式合成报告后，
相关109项及全量重新通过，没有弱化生产启动核验。`6957da9` 修复前一轮 CI 的
定时任务测试竞态：只等待已保存的 article 行，避免匹配尚未关闭的输入表单。

Linux真实验收：[6203411 的资源测试](https://github.com/Janettbella69/Open-Acciowork/actions/runs/35797207580/job/106979160537)
在 Ubuntu 24.04 上 **30 passed / 0 skipped**，11.97秒，明确启用
`OPHIRA_REQUIRE_SYSTEMD_TESTS=1`。包含23项进程/协议/故障测试和7项Linux验收，
实际触发CPU节流、64MiB内存OOM、16个进程/线程上限，并确认脱离原进程会话的后代停止。
首轮29通过/1失败源于故障注入仅断开用户bus，systemd仍可走私有socket；按官方v255
连接实现同时断开两条测试路径后全部通过，未改动或放宽生产限制。

Browser选择仍返回 `No browser is available`，故障检查返回 `[]`，页面目视验收未完成。
真实模型与Linux沙箱的联合运行、API宿主MCP资源治理、控制目录保留/容量策略、
生产长时负载及租户物理隔离仍需交付；总表其他 deferred 保持开放。


## 2026-09-22：Shopee 单店商品、SKU 与持久令牌刷新

实施合同：[Shopee seller reads and token rotation](shopee-connector.md)。通过官方公开
页面自身的文档接口核实签名、店铺、商品列表/详情、SKU与刷新合同，保留来源哈希和
示例字段不一致的说明；未使用第三方实现猜测接口，也未调用真实卖家服务。

- 新增单一店铺凭据配置、中英文页面、商品状态/分页、明确触发的SKU详情/分页，
  以及项目授权、原生SDK MCP和完整来源凭证。店铺与Partner ID以十进制字符串
  保存，避免前端int64精度损失；缺失详情不丢掉商品，价格精确到返回的十进制值，
  缺失库存保持未知，不当作零。SKU分页避免大量变体超出单次来源记录限制。
- 只访问已核验的固定官方origin与路径；公开DNS固定、无跳转/重试、每响应2MiB、
  整个操作30秒。多个响应的固定来源路径/请求ID共同保留，签名和凭据不进入来源。
- 可选刷新令牌在网络交换前持久领取一次性使用权，按vault版本更新新令牌；
  超时、取消、进程退出或异常响应保留待恢复状态，不自动复用可能已消费的令牌。
  请求途中撤销、相同密钥重新保存、配置替换均阻止旧结果返回或覆盖新凭据。
- Shopee专项 **72 passed**，包含真实两个spawn进程争用及进程突然退出；正式HTTP
  鉴权、原生MCP调用/来源验证和其他平台拒绝新增详情操作均覆盖。
- 冻结实现后的后端全量 **2292 passed / 20 skipped**，139.18秒；跳过仍为8项真实
  Provider、5项可选Chromium和7项仅Linux资源验收。前端全量 **458 passed / 46 files**，
  包含12项Shopee交互和1项页面路由测试。Ruff、diff检查、前端lint/build通过；
  eval工具 **59 passed**，Snapshot **61/61**。主JS包509.31kB的体积提示保留。
- Browser选择返回 `No browser is available`，故障检查列表为 `[]`，未完成目视验收。

尚需交付：Shopee初次卖家OAuth/授权码交换、订单和商业写入；已实现接口的真实
卖家账号/权限/地区/限流与模型验收；Temu/AliExpress、微信/桌面等总表剩余功能及
生产隔离和人工质量验收。完整PRD目标仍未完成。
