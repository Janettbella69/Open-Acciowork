---
name: market-research
description: 当用户要做市场调研、需求验证、竞品对照、趋势时机、公开数据检索或判断一个市场是否值得进入时使用。
version: 1.4.0
---

# Market Research

先写可证伪的假设，再检索公开来源。来源必须回链 URL 或用户材料路径。

## 触发

市场调研、需求验证、竞品、Google Trends、蓝海、热销、饱和、值不值得进、timing。

## 不要做

- 不把社媒热度写成已验证需求。
- 不把单个爆款视频外推成品类规模。
- 不在没有时间窗的情况下做“现在必须上”的判断。

## 步骤

1. 写下假设与目标市场；缺市场时用 AskUserQuestion。
2. 区分 observed_fact、user_statement、community_signal、inference。
3. 记录检索时间、口径与冲突；冲突并存时说明哪边证据更强。
4. 证据足够时，时机可用 Early / Peak / Saturated 表达，并写清所用代理信号与反证；不足时保留“无法判断”，不强制分档。
5. 产出：市场结论、证据清单、停止/继续条件。不代替用户做进入决策。

## 按需资源

- 设计检索与比较口径时读 [输入与证据规则](references/evidence-and-inputs.md)。
- 需要国家级进口、消费或人口的公开宏观参照时读 [世界银行 WDI 观察](references/world-bank-wdi.md)；不能把这些指标当作具体品类销量。
- 已有明确 HS6 编码、需核对欧盟成员国某类商品的年度进口贸易额时读 [Eurostat Comext 观察](references/eurostat-comext.md)；先核实商品归类，不能把进口额当作零售需求。
- 需要研究产物时，在项目工作区用 [市场假设与证据简报](templates/market-evidence-brief.md) 新建可编辑底稿；按问题删减模块，未知数据不以示例数字补齐，不修改 Plugin 模板。
