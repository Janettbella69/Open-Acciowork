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
| FR-11 领域 MCP | delivered：确定性商业计算、用户记录加工 | deferred：真实供应商/客户/市场/法规/物流/汇率授权数据适配、现金流模型 |
| FR-12 Skill 生命周期 | delivered：发现/启停/扫描/不可变版本/配额/租约/回滚 | deferred：真端点回滚重跑、备份恢复演练、每个内置 Skill 的证据参考与输出模板 |
| FR-13 权限 | delivered：范围/有效期/审计、删除确认、沙箱拒绝逃逸 | deferred：跨设备持久等待与真实远程审批验收 |
| FR-14 记忆 | delivered：提案人审、TTL/过期/检索/导出/删除恢复 | deferred：真实蒸馏质量、生产备份删除边界与恢复演练 |
| FR-15 自动化 | delivered：计划/心跳/收件箱本地链路；跨进程自动调度租约和崩溃幂等 | deferred：手动运行并发治理、真实长期运行与渠道投递 |
| FR-16 产物 | delivered：版本化台账/下载、Office 文件结构 | deferred：真实编辑流程、Office 渲染目视 QA、布局回归 |
| FR-17 可观测性 | delivered：trace、三层日志、脱敏、类型化卡片 | deferred：细分事件与统一指标/性能验收 |
| FR-18 恢复 | delivered：保存产物、错误分类、重试与重启恢复 | deferred：Bash 文件修改快照恢复与真实进程崩溃演练 |
| FR-19 Deep Research | delivered：恰好三个只读研究子代理、预算/取消/墙钟限制 | deferred：真实 SDK 执行、完整资源 UI 与合并证据验收 |
| FR-20 GUI | delivered：L1/L2 真实浏览器 smoke | deferred：真实攻击/恢复矩阵；L3 桌面桥与 OS 权限治理未实现 |
| FR-21 Connector | delivered：Catalog/Vault/health/revoke、HTTPS JSON send_message | deferred：邮箱/店铺/CRM/社媒垂直 adapter，真实授权 test-account 外发全链 |
| FR-22 组织治理 | delivered：单部署租户 principal/membership/策略 | deferred：租户分区、隔离 runner、共享项目/评论/模板、组织私有 Skill 仓库 |
| FR-23 Web 兜底 | delivered：Serper/Tavily 适配与 SSRF 防护 | deferred：真实配置搜索服务和 Provider 降级 E2E |
| FR-24 IM | delivered：飞书签名入站/身份绑定/同 Agent/持久回复重试 | deferred：真实飞书/Lark 租户 E2E；微信适配未实现 |
| FR-25 移动 | delivered：响应式 Run/审批控制面 | deferred：认证配对、可信设备、推送与跨设备真实验收 |
| FR-26 主动服务 | delivered：授权 standing intent、只读 watcher、预算与持久 claim | deferred：真实数据/Provider 长时判断质量、有效变化触达 |
| FR-27 媒体 | delivered：安全二进制接入与 Artifact/Evidence | deferred：真实图像/视频厂商适配、授权配置及验收 |
| FR-28 澄清表单 | delivered：结构化问题与同任务约束回传 | 保持 UI/API 回归 |

## FR 表之外仍属于全量范围的要求

| PRD 落点 | 状态及剩余工作 |
|---|---|
| §3.5 A–G / §6.5 | deferred：需求/供给/客户多起点真实基准、可编辑产物和连续对话验收 |
| §3.7 / §4.7 | deferred：由重复任务提出新 Skill，pending 人审 + 不可绕过扫描；不能自动发布 |
| §3.7 P2 | deferred：项目知识库索引；团队评论、模板和私有 Skill 管理 |
| §4.16 任务经验 | deferred：Schedule 专属经验积累，隔离于策略/全局记忆，不提升权限 |
| §4.12 事件 | deferred：产物更新、证据/决策/记忆和 schedule/preflight 细分事件 |
| §3.9 / §4.10 | delivered：自动调度跨进程抢占与事件汇总恢复；deferred：其余事件故障矩阵、Bash 修改恢复、CPU/内存/进程/输出限制 |
| §3.9 数据主权 | deferred：项目/会话/凭据的完整授权导出与删除（记忆已交付）；恢复策略及 UI |
| §3.9 性能 | deferred：Run 创建 P95 < 500ms，queued/started < 2s，取消 < 2s 的实测报告 |
| §3.9 / §3.10 | deferred：按 Provider/model/Skill/tool 的指标汇总、业务成功指标、区域与数据流披露 |
| §3.11 / §6.6 | deferred：当前 L2 基线、人亲自标注的 gold、judge 校准与 TPR/TNR 门槛；不能由 AI 伪造标注 |
| 教材审计 / CLAUDE.md | delivered：上传 PDF/图片生成 Markdown 伴随文件，pypdf/Tesseract 降级；deferred：当前 `_try_paddle` 使用 `PaddleOCR` 却标为 `paddleocr-vl`，需正确实现并验收 VL 摄取、明确提取失败边界 |
| P2-feedback-loop-spec.md | delivered：反馈存取 API、对话结果级按钮；deferred：消息/证据精确锚点、失败/取消自动采集、评测候选回流和人审处置；gold 本人标注与口径定版仍需负责人完成 |
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

后续代码复核：`MobilePage.tsx` 的控制/保活开关仍只写浏览器本地设置，
不构成认证、设备绑定、推送或系统保活实现。FR-25 不能因存在该页面而标为已完成。
`ingest.py` 和 `feedback.py` 已有部分代码，不能沿用历史审计的“完全未实现”描述，
上表已据现有实现细化其剩余缺口。
