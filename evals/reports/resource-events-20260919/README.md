# 产物版本与记忆提案事件验收

交付前基线：`e918d8b`。范围与保留项见 [实现合同](../../../docs/resource-events.md)。

| 验证 | 结果 |
|---|---|
| 后端全量 | 2148 passed / 13 skipped，129.00s；见 backend-tests.txt |
| 前端全量 | 485 passed / 45 files；见 frontend-tests.txt |
| Ruff、ESLint、TypeScript/Vite | 通过；主 JS 836.83kB / gzip 249.67kB，既有大包提示仍保留 |
| eval tools / 离线快照 | 59 passed / 61 of 61 |
| 原生 MCP 与 HTTP 专项 | 注册/更新/直接记忆提案/蒸馏、通知前后异常、回放游标、历史事件去重、跨 Run/scope/身份拒绝、删除与审批状态保持 |
| 真实进程恢复 | 写入进程退出码23；两个新 spawn 进程恢复三个资源，每项恰好一个事件 |
| 真实页面 | 中英文320/400/1280px、运行结束前通知、两版下载内容、记忆审核入口、刷新回放均通过，无页面异常 |

浏览器证据见 [browser-verification.json](browser-verification.json) 和本目录 PNG。
Browser 插件已有连接的发现结果为空，依其工作流使用仓库 Playwright/Chromium。
真实 Uvicorn/Vite 服务只使用临时账本；合成 runtime 经安装的 SDK 0.2.143 MCP
server handler 注册两个版本并提出记忆，无模型调用、外部账号、真实业务数据或真实凭据访问。

首次浏览器脚本把项目按钮的完整可访问名称误写成只有项目名，忽略其状态/日期副标题，
因此定位超时；读取实际页面后修正测试定位并取消那次合成任务，最终完整流程通过。
没有为使测试通过而修改产品行为。旧浏览器事件测试的通知总数断言也随新增的真实
Artifact 观察更新，仍断言每种业务记录只通知一次。

命令（仓库根目录；前端命令从 frontend 执行）：

```sh
PYTHONPATH=backend/src .venv/bin/python /private/tmp/ophira_conversation_pytest.py backend/tests -q
.venv/bin/ruff check backend/src backend/tests evals/tools
.venv/bin/python -m pytest evals/tools/tests -q
PYTHONPATH=evals/tools .venv/bin/python evals/tools/run_snapshot_evals.py
npm run test:run
npm run lint
npm run build
```

测试启动器沿用上一批隔离入口：创建 tempfile 数据目录并在导入 main 前替换
`config.get_settings`，再调用 pytest.main。临时 HTTP/浏览器验收脚本分别为
`/private/tmp/ophira_resource_fixture.py` 和 `/private/tmp/ophira_resource_browser_qa.py`。
持久回归已落在 `test_resource_events.py`、`test_resource_stream.py`、
`test_project_events_process.py` 及前端组件/hook 测试中。

这些结果不替代付费 Provider、远程存储、生产多租户或真实手机验收。
