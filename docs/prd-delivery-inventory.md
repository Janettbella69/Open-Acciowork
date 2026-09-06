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
| FR-11 领域 MCP | delivered：确定性商业计算、用户记录加工、显式结算计划的现金流/日末与保守日内资金缺口 | deferred：真实供应商/客户/市场/法规/物流/汇率授权数据适配 |
| FR-12 Skill 生命周期 | delivered：发现/启停/扫描/不可变版本/配额/租约/回滚；13个内置Skill的证据参考与输出模板、固定包原生只读访问；本地独立备份与新进程恢复演练 | deferred：真端点回滚重跑、跨宿主/生产备份恢复 |
| FR-13 权限 | delivered：范围/有效期/审计、删除确认、沙箱拒绝逃逸；可信设备单次审批；原生SDK持久等待、原Run恢复、跨进程决议竞争、消费前撤销复验 | deferred：真实远程审批与生产故障矩阵；原生多工具批次须拆为单次审批 |
| FR-14 记忆 | delivered：提案人审、TTL/过期/检索/导出/删除恢复 | deferred：真实蒸馏质量、生产备份删除边界与恢复演练 |
| FR-15 自动化 | delivered：计划/心跳/收件箱本地链路；跨进程自动调度租约和崩溃幂等；手动/自动触发共用租约与持久Run重叠检查、页面并发操作治理 | deferred：真实长期运行与渠道投递 |
| FR-16 产物 | delivered：版本化台账/下载、Office 文件结构 | deferred：真实编辑流程、Office 渲染目视 QA、布局回归 |
| FR-17 可观测性 | delivered：trace、三层日志、脱敏、类型化卡片；项目范围Provider/模型/Skill/MCP/错误分组指标与工作区面板 | deferred：细分事件与生产指标/性能验收 |
| FR-18 恢复 | delivered：保存产物、错误分类、重试与重启恢复 | deferred：Bash 文件修改快照恢复与真实进程崩溃演练 |
| FR-19 Deep Research | delivered：恰好三个只读研究子代理、预算/取消/墙钟限制；恢复沿用原起始时间、金额/turn预算及墙钟截止时间 | deferred：真实 SDK Deep Research 执行、完整资源 UI 与合并证据验收 |
| FR-20 GUI | delivered：L1/L2 真实浏览器 smoke | deferred：真实攻击/恢复矩阵；L3 桌面桥与 OS 权限治理未实现 |
| FR-21 Connector | delivered：Catalog/Vault/health/revoke、HTTPS JSON send_message | deferred：邮箱/店铺/CRM/社媒垂直 adapter，真实授权 test-account 外发全链 |
| FR-22 组织治理 | delivered：单部署租户 principal/membership/策略 | deferred：租户分区、隔离 runner、共享项目/评论/模板、组织私有 Skill 仓库 |
| FR-23 Web 兜底 | delivered：Serper/Tavily 适配与 SSRF 防护 | deferred：真实配置搜索服务和 Provider 降级 E2E |
| FR-24 IM | delivered：飞书签名入站/身份绑定/同 Agent/持久回复重试 | deferred：真实飞书/Lark 租户 E2E；微信适配未实现 |
| FR-25 移动 | delivered：项目范围配对、双端验证码批准、私有 cookie 与设备撤销/过期；单次持久审批、可选任务发起；Web Push订阅/加密VAPID/授权复验/持久重试与状态UI | deferred：native push、真实手机 HTTPS/外部Push/长期运行验收；OS 防休眠未实现 |
| FR-26 主动服务 | delivered：授权 standing intent、只读 watcher、预算与持久 claim | deferred：真实数据/Provider 长时判断质量、有效变化触达 |
| FR-27 媒体 | delivered：安全二进制接入与 Artifact/Evidence | deferred：真实图像/视频厂商适配、授权配置及验收 |
| FR-28 澄清表单 | delivered：结构化问题与同任务约束回传 | 保持 UI/API 回归 |

## FR 表之外仍属于全量范围的要求

| PRD 落点 | 状态及剩余工作 |
|---|---|
| §3.5 A–G / §6.5 | deferred：需求/供给/客户多起点真实基准、可编辑产物和连续对话验收 |
| §3.7 / §4.7 | deferred：由重复任务提出新 Skill，pending 人审 + 不可绕过扫描；不能自动发布 |
| §3.7 P2 | deferred：项目知识库索引；团队评论、模板和私有 Skill 管理 |
| §4.16 任务经验 | delivered：专属工具与版本/来源账本、创建时配置指纹、低信任检索注入、容量/事务恢复与人工停用UI；deferred：真实Provider自主调用及长期判断质量 |
| §4.12 事件 | delivered：有真实Run归属的evidence.added/decision.recorded、持久幂等补发与完整SSE尾部消费；deferred：产物更新、记忆、无Run项目变更和 schedule/preflight 细分事件 |
| §3.9 / §4.10 | delivered：自动调度跨进程抢占与事件汇总恢复；deferred：其余事件故障矩阵、Bash 修改恢复、CPU/内存/进程/输出限制 |
| §3.9 数据主权 | deferred：项目/会话/凭据的完整授权导出与删除（记忆已交付）；恢复策略及 UI |
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
