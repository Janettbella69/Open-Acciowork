---
name: shopify-store-review
description: 当用户要检查自己的 Shopify 店铺商品目录、近期订单、未发货积压或上新前状况时使用；数据只经项目授权的 read_marketplace 读取。
version: 1.0.0
---

# Shopify Store Review

只基于项目已授权店铺的真实读取做判断：先确认能读到什么，再描述看到了什么，最后才给建议。店铺自有数据不代表市场需求。

## 触发

Shopify 店铺体检、商品目录整理、草稿/下架/归档商品、近期订单概况、未发货积压、测试订单清理、上新前检查。

## 不要做

- 禁止猜测 binding_id、shop_id 或店铺地址；先用 `read_marketplace` 的 `operation=connections`。
- 禁止向店铺写入：改价、改库存、上下架、发货、退款都属于对外动作，本 Skill 只给建议草稿。
- 禁止索取或推断顾客姓名、邮箱、电话、地址；订单页本就不含这些字段。
- 禁止把一页数据说成全店；翻页不完整时写明已读页数。
- 禁止把最近 60 天订单外推为全年销量；`order_window` 为 `last_60_days` 时必须写明。

## 方法

1. 列出本项目授权。没有 Shopify 绑定时，请用户在 Plugins → Connectors → Shopify 配置店铺并授予项目权限，然后停下。
2. 商品用 `operation=products`，按需加 `product_status`（ACTIVE/DRAFT/ARCHIVED/UNLISTED），跟随 `next_page_token` 翻页并记录已读条数。
3. 订单用 `operation=orders`；读不到时说明连接器未允许 `read_orders`。区分测试单与已取消单，金额按 `currency` 分组，不跨币种相加。
4. 需要留痕的观察用读取返回的 `source_receipt.id` 调 `record_evidence`，一条证据只写一个可复核事实。
5. 输出顺序：观察（带读取时间与页数）→ 问题与影响（区分事实与推断）→ 建议动作（标明需人在 Shopify 后台执行或单独审批）。

## 按需资源

- 判断字段含义、覆盖范围和不能下的结论时读 [数据边界与核对](references/data-boundaries.md)。
- 需要交付店铺体检报告时，可用 [Shopify 店铺体检稿](templates/store-review.md)；复制到当前项目 workspace 后填写，不修改只读 Plugin 包。
