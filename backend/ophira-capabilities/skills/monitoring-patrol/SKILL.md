---
name: monitoring-patrol
description: 当用户要设置巡检、心跳、盯盘、竞品/价格/政策变化监控，或“有变化再告诉我”的常驻意图时使用。
version: 1.1.0
---

# Monitoring Patrol

- 巡检走 Scheduler；心跳任务若无变化只回复 HEARTBEAT_OK，不要刷收件箱。
- 常驻意图必须带冷却期、最大触发次数和过期时间；用 `create_standing_intent`。
- 凭据失效、运行失败必须通知；成功且无变化保持静默。
- 不在巡检里做跨包络承诺（新建长期自动化）除非用户本轮明确批准。

## 按需资源

- 定义变化、处理缺数据和核对授权范围时读 [输入与证据规则](references/evidence-and-inputs.md)。
- 用户需要可编辑说明或运行报告时，用 [巡检配置与观察记录](templates/watch-plan.md) 在项目工作区新建底稿。只保留配置或观察中需要的模块；报告不代替 Scheduler 的实际配置，也不提升权限。
