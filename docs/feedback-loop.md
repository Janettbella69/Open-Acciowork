# 反馈采集、人工复核与无标签评测候选

对应 `P2-feedback-loop-spec.md` 三个入口与两条回流；本实现不修改
PRD §3.11 / §6.6 的负责人本人 gold 标注与校准门禁。

## 已交付的本地链路

| Spec 要求 | 实现与证据边界 |
|---|---|
| ① 消息级结论问题 | Workspace「报告某条结论的问题」逐条展示已落盘的 assistant message；必须填写 why。服务器验证 `run_id` + `message_id` 是同一运行的 `assistant.message.completed`，不接受客户端伪造文本或 trace。 |
| ② 证据口径问题 | EvidenceDrawer 每条证据的报告按钮；服务器验证项目/证据与 `source_run_id`，没有来源 Run 的历史证据需要显式当前项目 Run 作追溯上下文。保存报告当时的证据快照，不改写 evidence/status/decision。 |
| ③ 失败与取消采集 | RunManager 终态后采集；区分用户取消与生命周期清理。每 Run 一个确定性 ID，跨进程文件锁与原子替换保证重复采集不重复建条；启动 reconciliation 修复终态和反馈之间的崩溃窗口。采集异常不改变 Run 结果或阻断其他条目的恢复。 |
| 人工复核 | `open → reviewed/rejected`；`reviewed → folded_into_skill/rejected/open`；`rejected/folded_into_skill → open`。每次需理由与 expected_revision，保留完整历史；并发过期表单 409。可信网关模式仅 owner/admin 且有项目权限可以复核。 |
| 同步候选标记 | 反馈 JSON 与服务器锚点、`candidate_state: unlabeled` 在同一次原子写中保存；候选不是判定，也不是 gold 标签。 |
| 候选回流 | 操作者从反馈面板显式导出**已加载**的非拒绝/有锚点条目，再用 `evals/tools/sample_feedback.py` 复用现有抽样流水线生成 annotation batch。失败/取消即使没有可抽取陈述也保留在 `feedback_candidates` 队列。 |
| Skill 人工经验沉淀记录 | 先 reviewed，后由负责人记录已经人工修改的 Skill ID/精确内容 hash、L2 报告引用与两项明确确认；API 验证版本确实在不可变历史中。不会修改、启用或发布 Skill。记录的是人工确认，不是机器证明 L2 达标。 |

## API

- `POST /api/feedback`：`kind`、非空 `user_note` 与精确锚点。结论需
  `run_id` + `message_id`；证据需 `evidence_id` + `project_id`/`run_id`；
  用户补充失败原因需已 failed/cancelled 的 `run_id`。
- `GET /api/feedback?project_id=...&status=open&limit=200&offset=0`：可选筛选，
  权限与筛选先于分页。`limit` 1–1000；不会因其他项目占满前页而隐藏当前项目反馈。
- `GET /api/feedback/{id}`：返回原始理由、服务器 context 和复核历史。
- `POST /api/feedback/{id}/review`：`expected_revision`、`status`、`note`；
  folded 还需 `skill_id`、`skill_content_hash`、`regression_report`、
  `manual_skill_update_confirmed: true`、`l2_regression_passed_confirmed: true`。

`context` 是服务器从真实 ledger 捕获的 run trace、event 与证据快照，调用者不得提交。
标记 stale/removed 后快照仍说明「反馈时看到的是什么」，不是伪装成现时有效证据。
历史无锚点条目可查看，不允许直接改成 reviewed 来虚构回链，需重新提交带锚点反馈。
本地模式沿用既有 loopback/Host/Origin 边界，不声称认证了本地操作者身份。
脱敏可能扩长原始文本；受限字段在脱敏后按原模型长度上限截断并保留
`[TRUNCATED]` 标记，不能因此拒绝原本合法的最大长度反馈或证据快照。

## 无模型、无自动标签的回流操作

1. 在项目「反馈复核与评测候选」打开并刷新条目；需要更多历史时点击加载更多。
2. 人工检查商业敏感性，点击「导出已加载的无标签候选」。导出不含认证 principal/session
   字段，仍可能含用户主动写下的商业内容，不能未经复核直接发布到开源仓库。
3. 从仓库根执行（路径由操作者选择）：

```bash
.venv/bin/python evals/tools/sample_feedback.py \
  --input /path/to/feedback-candidates.json \
  --out evals/gold/samples/feedback-batch.json
python3 evals/tools/annotate_gold.py \
  --samples evals/gold/samples/feedback-batch.json
```

候选脚本只依赖 stdlib 和 `gold_sampling`，不读取主系统数据目录、不调用模型、
不写 `labels.jsonl`、不覆盖现有批次；输入必须显式指定。既有人工标注界面仍由负责人
亲自作出判断。`feedback_candidates` 记录触发抽样的 why/trace/证据锚点，
`items` 仅为确定性启发式抽出的判定单元；零 items 不意味着报告被拒绝或 Run 质量合格。

## 仍未宣称完成的验收

- 首批 gold 由项目负责人本人标注、FM 工作簿定版、TPR/TNR/回归退化/积压阈值填定。
- 有真实用户反馈的 gold 抽样、人工改 Skill 后的真实全量 L2 回归及改善质量证据。
- 自动将生产商业 trace 复制进 `evals/gold` 的长期运行流水线：当前刻意保留显式人工
  导出与数据敏感性检查；不把候选标记误报为生产数据已入 gold 集。
- 「积压超过 __ 条」收件箱通知：原规格阈值未由负责人确定，不预填假数字。
- 浏览器真实操作/跨设备 UI 验收和运行中断故障矩阵仍需集成验收；本轮报告的测试为
  本地合成 fixture、HTTP 控制面、RunManager 实际异步生命周期与候选 CLI。

## 回归

```bash
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest \
  backend/tests/test_feedback_loop.py backend/tests/test_prd_features.py -q
.venv/bin/python -m pytest evals/tools/tests/test_feedback_sampling.py -q
cd frontend
npm run test:run -- src/components/FeedbackForm.test.tsx \
  src/components/FeedbackReviewPanel.test.tsx src/components/Workspace.test.tsx \
  src/components/EvidenceDrawer.test.tsx src/components/WorkspaceComposer.test.tsx
```

测试断言原始 why、精确 run/message/evidence 归属、角色与项目隔离、并发 revision、
状态历史、失败/取消幂等、私有文件与脱敏、UI 重试/切换、候选导出不含身份绑定字段、
复用抽样但不产生标签、CLI 原子落盘与拒绝覆盖。
