---
name: monitoring-patrol
description: 当用户要设置巡检、心跳、盯盘、竞品/价格/政策变化监控，或“有变化再告诉我”的常驻意图时使用。
version: 1.0.0
---

# Monitoring Patrol

- 巡检走 Scheduler；心跳任务若无变化只回复 HEARTBEAT_OK，不要刷收件箱。
- 常驻意图必须带冷却期、最大触发次数和过期时间；用 `create_standing_intent`。
- 凭据失效、运行失败必须通知；成功且无变化保持静默。
- 不在巡检里做跨包络承诺（新建长期自动化）除非用户本轮明确批准。
