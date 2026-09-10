# Deep Research 实际失败记录

2026-09-10 UTC（当地09-09），SDK 0.2.143，独立临时项目与三份合成上传材料。
Run `run_63c424e408054eb9b22398b796d29db6` 正常结束，但**研究验收失败**。

三个原生单元都启动了，然而 dontAsk 模式会拒绝启动配置中显式 ask 的只读工具。
每个单元的 Read/Glob/Grep/账本读取都被拒绝，返回 claims=[] 和工具拒绝说明。
主 Agent 后续自己的读取、证据和报告不构成子代理成功的证据。

SDK 估算1.849989 USD，上限2 USD，墙钟600秒，未发生商业外发。
原始判定在 [summary.json](summary.json)；加入合法 Glob/Grep 后的同档重验
[reverification.json](reverification.json) 仍失败，没有改写失败结果。

`events.jsonl.gz` 保存全部19,244条脱敏原生观察，压缩前后哈希见
[event-archive.json](event-archive.json)。来源文件、prompt、项目、Run、资源采样与
实际生成的报告均一并保留；报告本身不能用来声称研究验收通过。
