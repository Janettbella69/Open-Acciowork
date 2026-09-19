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
| FR-08 隔离 | delivered：项目路径、符号链接、独立 worker 配置 | deferred：生产 worker 资源包络与租户物理隔离演练 |
| FR-09 来源账本 | delivered：同 Run/session 的服务器 source receipt | 保持来源、失效、回放和内容完整性回归 |
| FR-10 决策 | delivered：证据关联与失效联动 | 开放任务质量另行验收 |
| FR-11 领域 MCP | delivered：确定性商业计算、用户记录加工、显式结算计划的现金流/日末与保守日内资金缺口；TikTok Shop项目指定店铺的主Agent只读MCP与来源凭证 | deferred：真实供应商/客户/市场/法规/物流/汇率授权数据适配及已实现商家接口的真实验收 |
| FR-12 Skill 生命周期 | delivered：发现/启停/扫描/不可变版本/配额/租约/回滚；13个内置Skill的证据参考与输出模板、固定包原生只读访问；Agent候选冻结、人审发布默认关闭、凭证恢复与独立审阅UI；独立Skill备份及含项目账本/候选库的应用冷备份、新进程恢复 | deferred：真实模型提案质量、真端点回滚重跑、跨宿主/生产恢复及其余子系统的恢复认证 |
| FR-13 权限 | delivered：范围/有效期/审计、删除确认、沙箱拒绝逃逸；可信设备单次审批；原生SDK持久等待、原Run恢复、跨进程决议竞争、消费前撤销复验；工作台内切换组织授权模式，仅 SDK 原生 default/auto 两档 | deferred：真实远程审批与生产故障矩阵；原生多工具批次须拆为单次审批 |
| FR-14 记忆 | delivered：提案人审、TTL/过期/检索/导出/删除恢复；跨进程scope事务、批准容量一致校验、中英文导出/续期/停用/确认删除UI与真实本地HTTP/浏览器验收 | deferred：真实蒸馏质量、生产备份删除边界与恢复演练 |
| FR-15 自动化 | delivered：计划/心跳/收件箱本地链路；跨进程自动调度租约和崩溃幂等；手动/自动触发共用租约与持久Run重叠检查、页面并发操作治理 | deferred：真实长期运行与渠道投递 |
| FR-16 产物 | delivered：不可变文件版本/准确历史下载、同内容回退的新版本、并发登记去重；文本预览/修改对比/冲突保护/幂等保存/人类编辑溯源、编辑中断恢复与冷备恢复；Office 文件结构 | deferred：真实模型连续编辑与浏览器目视、Office 渲染 QA、布局回归；旧记录已丢失的原始字节无法补造 |
| FR-17 可观测性 | delivered：trace、三层日志、脱敏、类型化卡片；项目范围Provider/模型/Skill/MCP/错误分组指标与工作区面板 | deferred：细分事件与生产指标/性能验收 |
| FR-18 恢复 | delivered：保存产物、错误分类、重试与重启恢复；Run 起点的完整可支持工作区快照、Bash 修改恢复、人工变更预览/确认/结果重试、中断目录切换修复、真实进程退出与冷备往返；同项目执行与产物收尾租约 | deferred：真实 SDK 故障注入、生产/跨宿主恢复、容量保留管理、性能与浏览器目视验收 |
| FR-19 Deep Research | delivered：输入框内显式开关发起（默认关闭、运行中锁定）；三种原生只读研究单元的一次新建/同 Run 续接约束；共享预算/取消/原始截止时间；运行中资源 UI、来源包与主 Agent 去重落账；真实 DeepSeek 合成来源上的三单元并行/来源复核/证据合并与报告验收 | deferred：开放互联网研究质量、真实取消/耗尽/长时墙钟故障矩阵、其他 Provider 与浏览器目视验收 |
| FR-20 GUI | delivered：L1/L2 真实浏览器 smoke；项目独立店铺 profile、用户登录确认、当前页观察引用、主Agent页面动作/报表下载与来源/产物落账、真实Chromium离线链路 | deferred：真实卖家后台/验证码/风控兼容性和模型联调；真实攻击/恢复矩阵；L3 桌面桥与 OS 权限治理未实现 |
| FR-21 Connector | delivered：Catalog/Vault/health/revoke、HTTPS JSON send_message；六平台官方入口，TikTok Shop授权店铺/商品与Shopify同组织自有店铺/商品/近期订单只读adapter、项目店铺授权/到期/撤销、主Agent来源凭证与中英文管理员UI；含Shopify的店铺身份配置、同平台Connector关联、独立浏览器授权及数据展示 | deferred：其余邮箱/店铺/CRM/社媒垂直adapter；TikTok Shop OAuth/refresh、Shopify组织外店铺授权、真实授权test-account及模型调用验收/外发全链；API与浏览器数据自动合并 |
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
| §3.7 P2 知识库 | delivered：项目登记文本/OCR伴随文件索引、来源行列/版本、权限复验API、只读MCP与中英文工作区检索、校验和一致的下载；deferred：Office原件索引、真实检索质量/容量与浏览器目视验收 |
| §3.7 P2 团队 | delivered：共享项目成员角色管理、关联项目/Run/消息/证据/决策/文件版本的团队讨论、项目模板保存/版本/参考文件下载/新建/归档/中断恢复、组织私有Skill审核发布与成员只读版本库及中英文UI；deferred：真实组织与浏览器目视验收、长期容量及生产隔离 |
| §4.16 任务经验 | delivered：专属工具与版本/来源账本、创建时配置指纹、低信任检索注入、容量/事务恢复与人工停用UI；deferred：真实Provider自主调用及长期判断质量 |
| §4.12 事件 | delivered：有真实Run归属的evidence.added/decision.recorded、持久幂等补发与完整SSE尾部消费；deferred：产物更新、记忆、无Run项目变更和 schedule/preflight 细分事件 |
| §3.9 / §4.10 | delivered：自动调度跨进程抢占与事件汇总恢复；Bash 工作区快照与人审恢复、跨进程目录切换修复；deferred：其余事件故障矩阵、生产恢复/容量与性能、CPU/内存/进程/输出限制 |
| §3.9 数据主权 | delivered：操作者CLI完整数据目录冷备份与新目录恢复，覆盖项目账本/提案/Registry/工作区检查点一致性；项目工作文件恢复UI；项目owner/admin审核清单后导出文件、应用可见会话、来源与相关账本ZIP；deferred：项目/会话删除（依赖审计与未实现设计见project-deletion-design.md）、凭据独立授权导出与删除（记忆已交付）、全应用恢复UI及生产灾备；项目ZIP不含运行时会话、凭据和浏览器登录资料 |
| §3.9 性能 | delivered：本地ASGI单/4并发各100样本，创建/首事件持久化/离线task取消P95基线；deferred：真实SDK进程、网络/SSE运输、多worker与生产负载验收 |
| §3.9 / §3.10 | delivered：项目范围按Provider/model/Skill/tool/错误的有界指标汇总；deferred：业务成功指标、区域与数据流披露 |
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

## 2026-09-19：工作台交互与连接器并发缺陷修复

交付前基线：`881a769`。本轮继续修复现有缺陷，完整 PRD 及前述 deferred 保持开放。

| 要求 | 状态 | 验证 |
|---|---|---|
| 保存授权模式期间不能用旧模式发送或重试任务 | delivered | 保存过程由工作台统一持有；按钮、快捷键及同一事件批次中的直接调用均阻止启动；保存失败保留草稿和旧模式，恢复控件 |
| 新任务不携带上一任务暂存的附件 | delivered | New task 清空附件；组件回归与真实浏览器附件操作通过 |
| 窄屏输入区可用，对话正文不被输入框遮挡 | delivered | 对话与输入框独立布局，窄屏输入区和按钮分行，附件列表有界滚动，菜单不越界；320–1440px 共 13 档浏览器检查通过 |
| 连接器健康检查不得覆盖撤销、配置或凭据更新 | delivered | 持久 revision 区分相同配置重连；锁内一致捕获配置/凭据，锁外探测，提交时复验；多进程修改与凭据写入串行化、独立临时文件原子发布 |
| 本轮真实模型、店铺、远程设备及生产验收 | deferred | 本轮使用离线运行时和临时本地账本，不替代这些外部验收 |

复现证据：原前端有 3 项新增回归失败；原连接器健康检查会重新启用已撤销的连接器、覆盖新地址。
独立复审进一步复现并修复了相同配置重连和保存凭据的竞争窗口。
原输入区在 440px/760px 宽度下仅剩 8px；修复后分别为 388px/460px。
交互测试覆盖 12 个附件、滚动到对话末尾、清空附件、真实策略 PUT 挂起期间的发送拦截；
没有创建模型 Run，策略在测试后恢复为 default。
浏览器测量与截图归档于 [本轮页面验证报告](../evals/reports/bugfix-20260919/browser-verification.json)。

验证命令与结果：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1895 passed / 13 skipped**（100.96秒）；相对本轮基线新增 6 项连接器回归。
- 前端 `npm run test:run`：**360 passed / 36 files**；新增 3 项工作台回归。
  `npm run lint`、`npm run build`（含 TypeScript）、`git diff --check` 通过。
- eval tools **59 passed**；离线 Snapshot **61/61**。
- `.venv/bin/ruff check backend/src backend/tests evals/tools --exclude project_export_capture.py --exclude project_export_models.py` 通过。
  不带排除项的全量 Ruff 仍有既存 9 项长行错误，全部在本轮开始前未跟踪的两个导出草稿中；
  未修改或纳入本轮提交。原有店铺浏览器未提交修改和 `deploy/companion/` 同样保留。
- 主 JS 783.51 kB，既有大于 500 kB 的构建提示保留。Browser 插件发现列表为空，
  页面复核使用仓库已有 Playwright/Chromium 与临时后端；不宣称真实手机或付费 Provider 验收。

## 2026-09-19：审核后项目 ZIP 导出与 Shopify 店铺身份

交付前基线：`5cb8379`。接续原有未完成的导出草稿和 Shopify 店铺身份改动，
交付范围以 [项目数据导出](project-export.md) 和 [店铺浏览器](shop-browsers.md) 为准。

| 要求 | 状态 | 验证与边界 |
|---|---|---|
| 项目数据可携带 | delivered | 本地操作者及当前项目 owner/admin 审核清单后下载 ZIP；保存文件、不可变版本、应用可见会话、来源、项目记忆及相关持久账本 |
| 导出前后内容与权限一致 | delivered | 绑定项目/主体/租户/角色/会话的审核指纹；下载重新捕获、复核权限和数据；源文件散列、跨项目来源、删除记忆、符号/硬链接与容量保护 |
| 导出界面与错误处理 | delivered | 中英文、分页清单、取消和项目切换保护；403/404/409 清除旧预览；真实忙碌提示；StrictMode 首次展开仅发送一次预览请求 |
| Shopify 店铺身份与窄屏显示 | delivered | 平台选择、可选同平台 Connector、创建/编辑/回读；不产生 API 或浏览器授权；160字符连续名称/ID 在320–1440px均不撑开卡片 |
| 项目/会话删除、凭据独立导出删除、运行时恢复与生产灾备 | deferred | 本项目 ZIP 不承担上述能力，原有全量 PRD 目标继续开放 |
| 真实 Shopify 登录/API、Provider、手机与生产多租户验收 | deferred | 本轮临时本地数据、测试卖家 DNS 和无模型运行时不能替代这些外部验收 |

发现并修复的具体缺陷：原导出草稿没有可用服务/API/UI；已保存店铺及已发布 Skill 候选的
严格日期字段不能从普通 Python 字典反序列化；损坏记忆历史和缺省汇总字段可能触发500；
Skill 候选的跨项目运行摘要、审核/清单/包散列以及商业动作和 receipt 来源必须一致核验。
共享存储按项目成员集合捕获，避免其他项目更新无故使当前清单失效。提取已有 Skill 候选
元数据检查供导出复用，没有新增模型运行路径或读取共享 Skill 库。

浏览器实测另复现并修复：StrictMode 重复 effect 的第一个请求虽被浏览器取消，后端线程仍
持有租约，第二次预览因此冲突；请求发起前改为微任务检查取消状态。超长店铺名曾让卡片达到
1191px，现四档视口下卡片宽均与容器一致。固定 DNS 测试只用于隔离环境的身份校验，生产
SSRF 检查保持原样；环境 DNS 代理返回198.18地址时仍正确拒绝保存，不因此关闭保护。

验证命令与结果：

- `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1980 passed / 13 skipped**（96.57秒）；导出 helper/service/API/integrity 专项 **85 passed**。
- 前端 `npm run test:run`：**384 passed / 38 files**；`npm run lint`、`npm run build`（含
  TypeScript）通过。主 JS 795.27kB，既有大于500kB提示保留。
- `.venv/bin/ruff check backend/src backend/tests backend/scripts/check_project_export.py evals/tools`
  全部通过，不再排除原来的导出草稿；`git diff --check`通过。
- eval tools **59 passed**；离线 Snapshot **61/61**。
- `PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_project_export.py`：真实独立
  loopback HTTP服务验证创建、上传、Shopify身份、审核失效和ZIP字节/散列；
  [HTTP报告](../evals/reports/project-export-20260919/http-verification.json)。
- 仓库 Playwright/Chromium 配合临时后端：真实下载附件名称、ZIP完整性与可见对话、内容变化
  后409与刷新、StrictMode单次请求、中英文界面、320–1440px店铺卡片及320/400/760px导出面板
  检查通过；[浏览器报告与截图](../evals/reports/project-export-20260919/browser-verification.json)。
  Browser插件发现列表为空，因此使用已有本地浏览器测试工具。

本轮未读取实际凭据目录、未调用真实模型或卖家接口、未启动店铺登录窗口。
`deploy/companion/` 原有未跟踪内容保留，不纳入本批交付。

## 2026-09-19：记忆生命周期与权限失效后的界面清理

交付前基线：`4978fb4`。本批交付 [记忆治理](memory-governance.md) 的用户入口与一致性修复。
[项目/会话完整删除的依赖审计](project-deletion-design.md) 仅为待实现设计，不能算作该功能交付。

| 要求 | 状态 | 验证与边界 |
|---|---|---|
| 并发记忆写入不丢失，删除后正文不被迟到写入恢复 | delivered | 跨实例/线程/进程可重入 scope 事务覆盖读写、回滚、删除、恢复及 Agent 镜像；删除审计读取锁内最新版本 |
| 批准容量限制不能经创建、回滚或续期绕过 | delivered | 所有有效 approved 写入在事务内校验原有32条/8000字限制；容量错误HTTP400，缺失版本404 |
| 记忆生命周期可由用户管理 | delivered | 中英文设置页提供JSON导出、1/7/30/90天或永久续期、停用及带理由的明确永久删除；取消不删除，过期项先续期再编辑/批准 |
| 私有记忆响应与界面清理 | delivered | 成功和错误响应no-store/nosniff；列表加载与变更互斥、取消/卸载后旧导出不触发下载、403/404清除私有条目与编辑内容，5xx保留重试上下文 |
| 项目权限失效不留下可用旧视图 | delivered | 权威项目403/404移除侧栏和当前项目/Run/对话/审批/指标，关闭流并阻止晚响应恢复；拒绝单次写入先复核项目读取权限，5xx不抹掉项目 |
| 用户未发送内容保留 | delivered | 权限撤销后保留输入草稿与本地附件；旧发送/重试响应不清除新草稿或覆盖提示 |
| 项目/会话完整删除、生产擦除与外部验收 | deferred | 原生SDK历史、共享模板/Skill来源、自动化/浏览器回写与备份恢复仍需跨域实现；本批不删除真实项目、不调用模型或外部服务 |

复现证据：旧实现中两provider同时新增只保留一个结果；迟到correct可在delete返回后把正文写回
items/history/MEMORY.md。新增回归还覆盖rollback、最新版本删除审计、恢复与新增竞争、并发批准、
过期列表读取与续期竞争。真实spawn进程在tombstone已fsync、快照rename前被SIGKILL，新实例能
获得OS释放的锁并完成正文/历史/镜像和遗留暂存清理。不能据此宣称存储损坏或远程备份擦除通过。

前端交叉审核发现并修复：初始/刷新列表尚未完成时，导出403或删除后可能被旧列表响应恢复；
客户端时钟落后不能覆盖服务器的expired判断。浏览器目视还补齐了中文删除按钮文案。

代码冻结后的验证：

- 后端 `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`：
  **1999 passed / 13 skipped**（118.51秒）。
- 前端 `npm run test:run`：**428 passed / 40 files**；`npm run lint`、`npm run build`
  （含TypeScript）通过；主JS805.50kB，既有超过500kB的构建提示仍保留。
- `.venv/bin/ruff check backend/src backend/tests backend/scripts/check_memory_lifecycle.py evals/tools`
  与 `git diff --check` 通过；eval tools **59 passed**，Snapshot **61/61**。
- `PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_memory_lifecycle.py`：两个独立
  HTTP worker共享临时账本，8项并发新增、8项改写、8项删除/续期和4项后续新增全部通过；
  重启后变更两个worker均可见，删除项的改写/回滚均404且正文不在快照/历史中。
  [真实HTTP报告](../evals/reports/memory-lifecycle-20260919/http-verification.json)。
- 仓库Playwright/Chromium与真实临时后端：逐请求签名的测试owner撤销member后，工作台清除
  旧私有结果并保留草稿/本地附件；实际JSON下载、续期、批准/停用、取消删除/确认删除、刷新及
  再导出均通过。中英文各320/400/1280px列表与18组编辑/续期/删除表单检查无水平溢出，已目视截图。
  [浏览器报告与截图](../evals/reports/memory-lifecycle-20260919/browser-verification.json)。

Browser插件发现列表为空，因此使用已有本地浏览器测试工具；测试身份签名不能冒充真实身份网关、
手机HTTPS或生产组织验收。未读取实际凭据目录、未创建模型Run。全量PRD目标及此前deferred继续开放；
原有未跟踪 `deploy/companion/` 保留。
