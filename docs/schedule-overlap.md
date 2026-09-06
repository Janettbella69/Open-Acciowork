# Schedule 手动与自动触发的重叠治理

同一个 Schedule 的 `POST /api/schedules/{id}/run` 与后台到期扫描共用既有
非阻塞跨进程 `.dispatch.lock`。锁覆盖读取当前计划、检查既有任务和创建 Run 的
异步过程，直到 RunManager 已持久化 Run 并启动本地任务；进程退出由 OS 释放锁。

释放 dispatch 锁后，服务器按持久化 Run 的 `project_id + schedule_id` 检查所有
非终态任务，不依赖本 worker 内存或 `Schedule.last_run_id`。queued、running、
waiting_permission、paused、cancelling 都占用该计划名额；只有 completed、failed、
cancelled 释放。不同计划和显式交互任务仍可并行。

## API 与页面行为

- 已有 dispatch：HTTP 409，提示刷新后再试。
- 已有非终态 Run：HTTP 409，提示等待完成或在工作区取消；不创建第二个 Run。
- 手动触发仍允许暂停中的计划；成功或重叠409均保留原有 next/last fire、enabled 与 last_run_id。
  它使用锁内重新读取的计划内容、既有权限上限、创建者权限和 Provider 就绪检查。
  Provider、策略或创建者权限已失效时，仍沿用既有暂停计划和错误通知行为。
- 自动触发发现手动任务在执行时，沿用已有重叠策略跳过本次并推进 next_fire_at；
  一次性任务因此停止，catch_up/skip 的恢复语义不变。
- 已持久化的自动 occurrence 仍先恢复原 Run 的审计与计划推进，不重复执行。
- 页面按计划禁用待处理操作；不同计划可并行，迟到的结果不能覆盖最新操作反馈。
  409 不自动重试，也不显示任务已启动；中文与英文均有对应提示。

## 验证与边界

测试使用临时账本和离线等待 runtime，包括实际 ASGI API、两控制面实例、真实 spawn
进程争用与进程终止恢复。覆盖锁持有期间、释放后、全部 Run 状态、无 last_run 指针、
到期/恢复/一次性任务、取消后再运行、权限回归及并发修改后的配置读取。

这项治理限制同一计划的任务重叠，不提供 HTTP 请求幂等键。一次任务已经结束后，
新的手动请求代表一次新运行。崩溃后保留的非终态任务须经既有 RunManager 恢复或
显式取消处理；手动请求不会擅自把它当成已结束。部署升级须先停完不遵守本锁协议的
旧 worker。网络文件系统锁、磁盘损坏、外部副作用 exactly-once、真实 Provider 长期
运行与渠道投递仍需独立验收。
