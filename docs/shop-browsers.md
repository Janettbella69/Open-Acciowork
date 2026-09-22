# 店铺配置、独立登录浏览器与 Agent 数据采集

交付范围：店铺身份配置 → 浏览器会话 → 主 Agent 任务 → 来源账本与文件 → 前端展示。
适用于运行后端的 macOS / Linux 桌面。店铺身份由操作者确认；本功能不验证平台实际登录账号。

```mermaid
flowchart LR
  Shop[项目店铺身份] --> Connector[平台 Connector]
  Connector --> API[授权 API 读取与操作适配]
  Shop --> Browser[独立登录浏览器]
  Browser --> Confirm[用户登录并确认店铺]
  Confirm --> Agent[Claude Agent SDK 主 Agent]
  API --> Agent
  Agent --> Ledger[来源凭证 / Evidence / Artifact]
  Ledger --> UI[项目数据展示与文件下载]
```

平台 Connector 承担应用身份、OAuth / API Key / Token 及平台 API 适配；店铺记录保存
平台、外部店铺标识、可选 Connector 关联和浏览器范围。上图中的 API 能力按平台逐项交付，
具体状态见 [平台 Connector 清单](marketplace-connectors.md)。目前 TikTok Shop 具有手动
配置卖家 Token 和授权店铺/商品读取能力，OAuth 回调、Token 交换/刷新及平台商业写入仍待交付。
其余四个平台已有官方文档入口，不能据此宣称接入完成。

浏览器与 API 分别授权。关联 Connector 不会自动创建 API 店铺授权，也不会把 API
授权转换成浏览器会话授权。浏览器产物记录内部 shop_id、外部 seller_shop_id、platform
和 connector_id，便于追溯；当前数据面板列出浏览器采集，尚未自动合并 API 商品记录。

## 使用

先安装后端已有的可选浏览器依赖和 Chromium；生产窗口保持 Chromium 自身沙箱开启：

```sh
.venv/bin/python -m pip install -e 'backend[browser]'
.venv/bin/python -m playwright install chromium
```

1. 在已保存项目的工作区打开 **Project shops / 项目店铺**，填写显示名称、平台、外部店铺
   标识、卖家后台 HTTPS 地址及需要的登录/CDN 域名。入口不接受凭据、查询参数或片段。
2. 可选关联同平台 Connector，保存后选择 **Open shop login browser / 打开店铺登录浏览器**。
   窗口出现在运行后端的电脑上。远程网页不会把窗口传输到访问者电脑。
3. 用户亲自完成登录、验证码和店铺切换，核对当前窗口属于该店铺；回到工作区勾选确认，
   授权 1、7 或 30 天。检测到可见密码/OTP 输入框或登录路径时拒绝授权和 Agent 采集。
4. **Prepare shop task / 准备店铺任务** 只填入任务草稿。用户提交后，由已有主 Agent 执行。
   读取页面可使用已授权会话；点击、填写、提交和下载报表仍逐次经过原生 SDK 审批。
5. **View collected shop data / 查看已采集店铺数据** 显示最近 50 次采集，提供来源地址、
   采集时间、截图、页面文本和原始报表下载。页面文本也进入项目现有知识检索。

关闭窗口会结束当前会话；重新打开可保留登录状态，但必须重新确认 Agent 授权。编辑身份
前须关闭窗口；更换平台、外部店铺标识、Connector 或入口主机将启用新的浏览器配置目录。
撤销阻止后续 Agent 使用，同时保留用户登录窗口和已采集项目数据。权限冲突后先刷新状态。

## 执行与来源

沿用现有 `ophira_browser` MCP 的两个工具，无新增模型循环或独立业务 Agent：

- `snapshot_page`：`list_shops: true` 分页发现当前项目店铺；`shop_id` 读取当前会话；
  可选 `url` 在该店铺允许的域名中导航。主 Agent 获得 observation_id 和 element_N 引用。
- `act_on_page`：传入 shop_id、最近的 observation_id、作为 selector 的 element_N，
  以及 click/type/submit/download 动作。拒绝猜测 CSS 选择器、过期页面和已变更元素。
  研究子代理不能直接使用这些会写入项目来源的店铺工具。

每店铺最多一个持久 Chromium 进程。私有 Unix socket、随机会话密钥和 OS 文件锁实现
所有权与串行操作；SDK 工具参数不能设置 socket、CDP 地址、Cookie、profile、Run 或 SDK 身份。
后台在队列消费时、操作执行期间、网络检查和保存结果前复验项目成员、运行状态、SDK 会话、
店铺 revision、当前会话及授权到期/撤销。客户端断开或授权变更会取消进行中的操作；
已发出的远端副作用无法回滚。用户在窗口内手动操作仍由用户负责。

截图、页面文本与报表通过同一个真实 Run 归档到 `workspace/shop-captures/`。来源凭证绑定
Run/SDK session、店铺、URL、时间和文件 SHA-256；报表经过字节大小及校验和验证才登记。
报告保留原始文件字节，显示经过清理的下载文件名；不会自动把报告内容转换为销售额等指标。
来源 URL 移除查询与片段；页面文本、文件名和工具输出遵循现有外部不可信数据处理规则。

## 边界与运维

- 每项目最多 100 个店铺；每次最多一个报表，支持 csv/tsv/xlsx/xls/pdf/json/zip，单文件
  20 MiB；报告原件不自动执行或解压。浏览器截图保留当前视口，文本和元素列表有明确上限。
- 每次动作最多 40 秒，下载等待最多 20 秒；临时文件受大小限制，失败时清理，旧临时文件
  在后续操作时清理。未通过 download 操作发起的下载会取消。
- 域名范围与当前组织策略取交集，并复用 DNS 固定连接和私网地址拒绝。弹窗和 Service Worker
  被禁用。依赖这些能力的卖家后台可能需要后续适配；不能宣称五平台登录流程均兼容。
- `data/shop-browsers/` 保存私有配置、登录文件和会话元数据，位于项目工作区之外；主 Agent
  与预检的 SDK 沙箱配置显式禁止读写此目录。普通 Read/Grep 继续受现有项目路径门禁约束。
  这是本地宿主边界，尚未完成生产多租户物理隔离和真实 SDK 沙箱攻击认证。
- 浏览器进程持有应用维护租约；即使 API 进程停止，活动窗口仍阻止冷备份。先关闭所有
  店铺窗口再备份。冷备份包含登录文件，须按敏感应用数据保管；跨机器登录恢复未经认证。
- UI 不接收密码或 Cookie。无人提供真实卖家账号时，不能确认当前平台授权、验证码、
  风控兼容性或真实报表格式。后台必须运行在用户能操作其显示器的环境。

## API

所有接口属于 `/api/projects/{project_id}/shops`，响应禁止缓存；可信网关模式沿用项目
身份检查，配置、窗口控制、确认和撤销要求 owner/admin，读取列表/数据要求项目读权限。

| 方法与路径 | 功能 |
| --- | --- |
| GET / POST 基础路径 | 列表 / 创建身份 |
| PUT `/{shop_id}` | 编辑，携带 expected_revision |
| POST `/{shop_id}/open`、`/close`、`/revoke` | 窗口控制或撤销，携带 expected_revision |
| POST `/{shop_id}/grant` | 当前 revision、session_id、confirmed_identity=true 和有效秒数 |
| GET `/{shop_id}/data` | 最近 50 个采集记录及已登记 Artifact 下载链接 |

## 验证

常规后端测试不启动浏览器。以下独立测试使用真实 Chromium、DOM、下载事件、持久 profile
和私有 socket，网络内容全部为合成卖家页面；既不连接平台，也不调用真实模型：

```sh
OPHIRA_SHOP_BROWSER_E2E=1 PYTHONPATH=backend/src .venv/bin/pytest backend/tests/test_shop_browser.py -q
```

覆盖会话隔离/重开、登录墙、报告字节与来源回放、项目隔离、真实元素失效、socket 密钥、
原生 MCP 与 PostToolUse 内容一致性、主 Agent 限制及逐次动作审批。权限取消、成员撤销、
配置 CAS 和生产 HTTP 路由由 `test_shop_browser_access.py` 覆盖；UI 行为由
`ProjectShopsPanel.test.tsx` 覆盖。真实平台、真实模型和人机登录验收仍待完成。

实现参考：[Playwright 持久浏览器](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、
[下载处理](https://playwright.dev/python/docs/downloads)、
[Claude Code 沙箱文件范围](https://code.claude.com/docs/en/sandboxing#configure-sandboxing)。
