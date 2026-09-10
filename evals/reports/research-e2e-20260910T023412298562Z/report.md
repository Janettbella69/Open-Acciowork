# Deep Research 真实 SDK 执行与归档复验

Run `run_c2df278aac414b20a6e5d742e56c946c`，2026-09-10 UTC（当地09-09）。
SDK 0.2.143，主消息模型 deepseek-v4-pro；子任务原生返回的 resolvedModel 为
deepseek-v4-flash。材料全部为合成 fixture，未做互联网搜索或商业外发。

修复原生 dontAsk/ask 冲突后，三个研究单元实际并行运行、完成9次原件 Read，
返回非空来源包；主 Agent 重新读原件、去重后记录两份报价、关联决策和不可变报告。
任务完成，估算1.920009 USD，上限2 USD，约180秒，原始截止600秒，轮数不限。

| 研究单元 | 原生任务 ID | 最终累计 token | 工具数 | 耗时 ms |
|---|---|---:|---:|---:|
| source-mapper | a8936899a0d8e7568 | 4329 | 4 | 17196 |
| multilingual-researcher | ac8cb33a96aad3fe7 | 4299 | 5 | 9619 |
| conflict-verifier | a8e6476d9ae9d1690 | 5452 | 6 | 17946 |

以上是各自最后的原生任务快照，不相加为 Run token/费用。16,520条事件完整回放；
资源快照逐项匹配原生最终通知，报告字节匹配项目不可变产物 SHA-256。

[原始 summary.json](summary.json) 保留唯一误报：验收器遗漏合法只读 Glob。
修正验收器允许 Glob/Grep 文件定位后，对**同一份归档**执行的
[reverification.json](reverification.json) 全项通过，无新增模型调用。
修改没有放宽实际 Read、三单元/并行、只读权限、外部工具、证据或引用要求。

[实际报告](deep-research-report.md)、项目/Run、prompt、fixtures、资源采样均已保留。
`events.jsonl.gz` 是完整脱敏事件的无损压缩；校验信息见
[event-archive.json](event-archive.json)。SDK 费用是估算，不能当厂商实际账单。

这不证明真实商务来源、开放研究质量、其他 Provider、真实取消/耗尽/长时故障、
生产多租户或浏览器目视验收通过。
