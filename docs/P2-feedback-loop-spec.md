# P2 立项：数据反馈闭环（feedback loop）

> 状态：**立项 spec（2026-08-24），未排期、未实现。** 教材第 13 章对照审计发现的唯一缺失项：
> "用户发现 Agent 错了，能在界面上告诉你吗？那条反馈会变成 Skill/评测集吗？"——当前答案是不能。
> 本文只定结构与口径，门槛数值留空，跑出基线后填定（同 §3.11 纪律，不预填演示数字）。

## 一句话

闭环的主语是认知：用户反馈 → 落账本 → 回流评测集 → （经人审）沉淀为 Skill 经验，
让系统从"工具"变成"客户赖以运行的系统"（a16z system test）。

## 范围（三个入口，两条回流）

### 入口

| 入口 | 面向谁 | 反馈什么 | 前端挂点 |
|---|---|---|---|
| ① 结论有误反馈 | 终端用户 | 这个判断/数字看着不对 + 为什么 | 对话消息级按钮（WorkspaceComposer 的 assistant 消息） |
| ② 证据问题反馈 | 用户/分析型用户 | 来源失效、口径不一致、历史遗留坑 | EvidenceDrawer（已有 stale/removed 操作，扩展"报告口径问题"） |
| ③ 运行失败自动采集 | 系统 | run.failed / 用户手动取消的 run | 无 UI，后端自动 |

### 回流

1. **进评测集**：反馈关联的 run trace 自动标记为 gold 候选，进入 `evals/gold/` 抽样池
   （复用 sample_gold.py 流水线；线上失败样本自动进评测集 = 教材第 12 章〔补〕）。
2. **沉淀为经验**：聚合后的反馈由负责人复核，人工决定是否写入对应 Skill
   （"把限制变成业务经验"，绝不自动改 Skill——Skill 改动必跑 L2 回归）。

## 数据结构（草案，实现时定稿）

```
feedback/
  {feedback_id}.json:
    run_id / project_id / message_id | evidence_id   # 定位
    kind: conclusion_wrong | evidence_issue | run_failure
    user_note: str                                    # 为什么不对（必填，①②）
    created_at / status: open | reviewed | folded_into_skill | rejected
```

落 FileLedger 同款 JSON 原子写；不进 `ophira_backend` 运行时决策路径（反馈不改变
当前 run 行为，只做事后回流——避免变成代码门禁）。

## 验收口径模板（数值留空）

- 反馈提交到进入 gold 候选池的链路延迟 ≤ __ （目标：同步落盘）
- 每条 reviewed 反馈可回链到 trace + 证据条目（可回链率 = 100%，硬口径）
- Skill 因反馈修改后，L2 回归全量跑通且关键指标不劣化 ≥ __%
- 反馈→复核的积压上限 __ 条（超过则在收件箱提醒负责人）

## 边界（防走形）

- 反馈**不触发**自动改 Skill/Prompt/代码——人审是唯一通路（人把关，不是代码把关）。
- 评测回流侧代码只住 `evals/tools/`；backend 只新增反馈存取 API 与前端挂点。
- 不做点赞/点踩这类无 why 的信号——反馈必须带"为什么"（教材 13 章：要让用户说清
  "这个数据从原理上哪里就不干净了"）。

## 依赖与顺序

前置：gold 首批本人标注完成（当前用户闸门）、FM 工作簿复核定版。
实现顺序：③（纯后端，最小）→ ②（扩展既有 EvidenceDrawer）→ ①（新 UI 挂点）。
