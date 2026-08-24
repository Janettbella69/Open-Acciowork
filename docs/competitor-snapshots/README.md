# 竞品能力面快照 · Accio（2026-08-24 08:44 UTC）

> 数据源：`/Users/isabella/.accio/accounts/1780279321`（本机 Accio 客户端缓存，账号已登录）。
> **只收录元数据与内容哈希，不复制任何 Skill 正文**（沿用 `competitive-reverse-engineering-2026-08-21.md` 的纪律）。
> 机器可读全量：`accio-inventory-20260824T084414Z.json`（313,903 B）。仓库为 private。

## 为什么要定点留档

Accio 客户端在后台持续同步、覆盖、删除本地能力包，任何"当前数量"都只是切片。同一晚实测到的漂移：

| 观察 | 变化 |
|---|---|
| 账号级 Skill | 31 → 124（约 30 分钟内拉全整个目录） |
| 已安装插件 | 15 → 18 → 19（新增 feishu-assistant / cocreate-pitch / logistics-expert / yuque / mydepot） |
| accio-site-builder 内嵌 Skill | 38 → 16（重装后精简） |
| pickfu | 授权卡住时 21 个文件已完整落盘 → 点 Cancel Install 后**整目录被删除** |

**取包窗口**：安装流程里 `Initialize resources ✅` 完成后、授权完成或取消之前，包是完整可读的；取消即回滚删除。

## 当前规模

| 项 | 数量 |
|---|---|
| 本地 SKILL.md 文件 | **383** |
| 去重后不同 Skill | **340** |
| 已安装插件 | **18** |
| 远端 Skill 目录（官方） | 123 |
| 远端插件目录 | 71（OFFICIAL 54 / THIRD_PARTY 17） |

## 插件知识密度（决定研究优先级）

| 插件 | Skill | 正文字符 | MCP | 子Agent | 性质 |
|---|---:|---:|:-:|:-:|---|
| `alibaba-com-seller-assistant` | 52 | 693,697 |  | ✓ | 厚知识 |
| `accio-com-buyer-assistant` | 36 | 557,818 |  | ✓ | 厚知识 |
| `ecommerce-automation-toolkit` | 19 | 311,246 |  |  | 厚知识 |
| `feishu-assistant` | 27 | 249,184 |  |  | 厚知识 |
| `shopify-plugin` | 15 | 230,136 |  | ✓ | 厚知识 |
| `accio-site-builder` | 16 | 162,560 |  | ✓ | 厚知识 |
| `geo-agent` | 12 | 96,580 |  |  | 厚知识 |
| `global-ecommerce-design` | 6 | 69,867 |  |  | 厚知识 |
| `1688-purchasing-assistant` | 4 | 36,034 |  |  | 中等 |
| `documents` | 2 | 27,563 |  |  | 中等 |
| `okki-crm` | 4 | 26,391 |  |  | 中等 |
| `presentations` | 1 | 23,056 |  |  | 中等 |
| `spreadsheets` | 1 | 19,705 |  |  | 中等 |
| `shopline-plugin` | 5 | 17,060 |  |  | 中等 |
| `knowledge-base-plugin` | 1 | 11,592 |  |  | 中等 |
| `yuque-plugin` | 1 | 5,663 |  |  | 薄壳/连接器 |
| `mydepot` | 1 | 3,957 |  |  | 薄壳/连接器 |
| `cocreate-pitch-2026` | 0 | 0 |  |  | 薄壳/连接器 |

## 业务域分布（去重后 340 个 Skill）

| 业务域 | Skill 数 | 正文字符 | 我们的覆盖 |
|---|---:|---:|---|
| 选品与市场情报 | 66 | 741,857 | ✅ `global-opportunity-execution` |
| 营销与广告 | 61 | 692,284 | ❌ 空白 |
| 供应商与采购 | 52 | 673,840 | ✅ `supplier-due-diligence` |
| Listing 与 SEO | 33 | 312,059 | ❌ 空白 |
| 店铺与平台运营 | 26 | 344,177 | ❌ 空白 |
| 办公文档 | 22 | 195,281 | ❌ 空白 |
| 物流关务与合规 | 19 | 149,267 | ❌ 空白 |
| 客户与 CRM | 17 | 151,511 | ✅ `customer-path` |
| 内容与创意 | 12 | 110,842 | ❌ 空白 |
| 建站与开发 | 10 | 54,602 | ❌ 空白 |
| 其他 | 8 | 61,124 | ❌ 空白 |
| 财务与定价 | 8 | 77,482 | ✅ `landed-cost-analysis` |
| 元能力与研究 | 6 | 98,364 | ❌ 空白 |

## 与 Open-Acciowork 四个 Skill 的对位

| 我们的 Skill | Accio 对应密度 | 判断 |
|---|---|---|
| `global-opportunity-execution` | 选品与市场情报 66 个 / 741,857 字符 | 他们拆得极碎（蓝海/热销/深度验证/趋势时机各一），我们一个 Skill 统包——**教材口径我们对**，但口径颗粒度可借鉴 |
| `supplier-due-diligence` | 供应商与采购 52 个 / 673,840 字符 | 他们有 1688/国际站原生数据通道，我们靠公开检索+用户材料——**数据源劣势，方法论可平** |
| `landed-cost-analysis` | 财务与定价 8 个 / 77,482 字符 | 我们有确定性计算 MCP 兜底口径，他们多为提示词——**这一格我们更硬** |
| `customer-path` | 客户与 CRM 17 个 / 151,511 字符 | 他们含 CRM 授权数据（okki），我们只到公开信息层 |

## 结构性空白（他们有、我们零覆盖）

- **营销与广告**（61 个 / 692,284 字符）
- **Listing 与 SEO**（33 个 / 312,059 字符）
- **店铺与平台运营**（26 个 / 344,177 字符）
- **办公文档**（22 个 / 195,281 字符）
- **物流关务与合规**（19 个 / 149,267 字符）

判断：这些空白多数**不该补**——Listing/广告/建站/办公文档属于"平台运营执行"，与本产品"可追溯的机会判断"定位不同域。
值得关注的只有**物流关务与合规**（跨境刚需、且与落地成本口径直接相连）。

## 可迁移的形态（学结构，内容自己写）

1. **NEVER DO 负面清单**：`cross-border-selection` 开篇六条 ❌，把踩过的坑写成硬禁令——比正面描述更能压住模型走捷径；
2. **HARD-GATE 分阶段检查清单**：准备/抓取/校验/保存四阶段各带 ✅ 项，末尾用可验证等式收口（记录数 = 各商品 SKU 数之和）；
3. **Intent Decision Tree**：入口意图 → 分支 → 每分支的必经动作，写在 Skill 顶部；
4. **triggers + examples 显式列举**：`alibaba-blue-ocean-finder` 用 9 个触发词 + 5 条示例问法 + 明确的"不处理什么"来划边界——比纯 description 触发准确得多；
5. **插件自带 subAgents**：PickFu 的 `pickfu-researcher` 是插件级支线子 Agent 的产品化形态，可作我们"普通模式支线子 Agent"拍板的参照。

## 权属与纪律

- 目录混着两类：**Anthropic 开源 Skill**（pdf/docx/xlsx/pptx/skill-creator 等，可直接复用）与**阿里自研业务 Skill**（`ownerType: official`，占绝大多数）。
- 本快照及后续研究**只提取结构与方法论形态**；阿里自研内容不得逐字进入本产品的 Skill 或提示词。
- 复查漂移：重跑快照脚本对比 `sha256` 与 `counts` 即可（见下节）。

## 复跑与漂移比对

```bash
python3 docs/competitor-snapshots/snapshot_accio.py docs/competitor-snapshots
```

脚本只读 `~/.accio/`，遍历全部 `SKILL.md` 按 `skills/`、`plugins/installed/`、`agents/` 三类归属，
提取 frontmatter 的 name/description、字节数、内容 sha256 前 16 位、mtime，并读取 `plugin.json`
与两份远端目录缓存。**不复制正文**，输出一份带时间戳的 JSON；比对新旧两份的 `counts` 与
`sha256` 即可看出 Accio 侧的增删改。它是研究工具，与产品运行时和评测系统均无耦合。
