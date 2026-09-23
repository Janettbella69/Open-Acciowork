# Temu local-seller reads

Implemented for FR-11 / FR-21 against public Temu documentation reviewed on
2026-09-22. Contract, HTTP, native SDK MCP and UI tests use synthetic records.
Live seller, regional, model and browser visual acceptance remain outstanding.

## Configure and read

Open **Plugins → Connectors → Temu**, select the app/shop region and supply the
existing `app_key`, `app_secret` and seller `access_token`. Inputs are masked and
cleared after saving. Save enables configuration; **Read store** verifies current
shop identity, seller type, API scopes and absolute expiry. Replace expired tokens
in the same form. The same JSON can be supplied through
`OPHIRA_CONNECTOR_TEMU_KEY`; vault deletion persistently disables that fallback.

Fixed origins are `https://openapi-b-us.temu.com`, `https://openapi-b-eu.temu.com`
and `https://openapi-b-global.temu.com` (other regions). No automatic regional
fallback is attempted. All calls are signed JSON `POST /openapi/router`.

Local permissions are `temu:store:read`, `temu:products:read` and the separately
opted-in `temu:supply-prices:read`. Required APIs must also appear in the token's
current upstream `apiScopeList`; local settings do not grant upstream authority.
Token info is fetched on every operation, including product/SKU pages.

It returns numeric `mallId`, `mallType` (100 local, 1 semi-managed), `regionId` and
absolute Unix-second `expiredTime`, with no shop name or ISO country. IDs become
exact strings; missing names/countries remain empty. Semi-managed identity can be
inspected, but catalog reads currently require local sellers. Both server and UI
reject unusable project product grants.

## Contracts and data meanings

| Read | Official API | Permission |
| --- | --- | --- |
| Shop and token info | [bg.open.accesstoken.info.get](https://partner.temu.com/documentation?menu_code=93de550b56c8417caccb88824be3e614) | Local store read |
| Products / one parent | [temu.local.goods.list.retrieve](https://partner.temu.com/documentation?menu_code=ce4883149f784c208e1b9a52566ea2c8) | Local product read + upstream product API |
| SKU page for that parent | [temu.local.sku.list.retrieve](https://partner.temu.com/documentation?menu_code=3709c38a08624375bbbe8b6440e41475) | Above + upstream SKU API |
| Supply prices for returned SKUs | [bg.local.goods.sku.list.price.query](https://partner.temu.com/documentation?menu_code=a9bd154c38384f2c99e57c2ebe271299) | Above + local and upstream price permission |

The shared API supports `authorized_shops`, `products` and `product_details`.
Products require `shop_id`, page size 1–25 and an optional opaque `page_token`.
UI/Agent pages are capped at 10. Details additionally require `product_id`; their
cursor pages through SKUs for that parent. The parent is fetched independently,
and parent/SKU associations are verified. Tokens are passed unchanged.

Product filters: ALL, ACTIVE, INACTIVE, INCOMPLETE, DRAFT and DELETED.
**ALL means active plus inactive only**. Drafts, incomplete and deleted products
require separate reads. Product lists expose metadata and reported variant counts.
Explicit SKU reads add merchant SKU, status, named options and `reported_stock`.
That stock has no documented available/reserved breakdown.

`supplier_price` means latest **supply price**, not retail price, revenue, profit
or market demand. Amounts remain decimal strings with returned currencies; no
minor-unit or currency conversion is inferred. Missing stock/prices and partial
price objects stay null, never zero. `supplier_price_access` distinguishes
disabled, not_authorized, allowed and unsupported_identifier. New catalog APIs
use string IDs while the older price API requires LONG IDs: nonnumeric/out-of-int64
IDs remain readable but skip price calls with an explicit partial-data indication.

`variants_complete` requires a whole first-and-only SKU page matching its total.
A terminal first page with omitted rows, or missing requested prices, sets
`partial_data`; ordinary pagination alone does not mean missing data.

## Authority and provenance

**Project access for Agents** grants the selected shop for a fixed duration after
server re-verification. Revocation remains available even if current product reads
are unavailable. Main-Agent `read_marketplace` uses the immutable project binding,
not caller-supplied shop URLs or arbitrary API names. Platform, origin and app key
bind connector identity. Replacement tokens for the same app can retain a grant;
a token for another mall cannot use it.

Every network boundary rechecks project/run/session authority, configuration and
vault generation, including same-value key replacement. Expiry is checked again
before returning. Changed/revoked authority discards the observation. Source receipts
retain every fixed gateway URL, API name and optional upstream request ID, excluding
secrets/signatures/request bodies. Missing IDs stay empty. Business text is sanitized
as untrusted. Receipts do not automatically create evidence or decisions.

The shared transport pins public DNS while preserving Host/SNI, forbids redirects,
bounds each response to 2 MiB and the operation to 30 seconds, and performs no HTTP
retry. Duplicate-key, malformed, foreign-ID and oversized responses fail with fixed
credential-safe errors. HTTP reads retain owner/admin guards and `no-store` headers.

## Official evidence and discrepancies

Public documentation came from the page's own `POST /riemann/api/v2/menu/list`,
`/riemann/api/v2/api/doc/detail/get` (`api_id`) and
`/riemann/api/v2/menu/document/detail` (`menu_code`) on `https://partner.temu.com`.
No login, seller credentials or business API request was used in this research.

- The [gateway guide](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=8311de2b2d434e4d805e88413ab815d8) supplies the regional origins.
- [Signing](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=4a821c90d06442a09e061b0d4316fbf3) and the [Python example](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=89e9f25227e5496bbbd894cbfac9c4fa) specify sorted outer keys, compact JSON values, secret prefix/suffix and uppercase MD5. Nested insertion order is preserved. The published vector inconsistently reorders nested fields in steps 1/5; the exact step-2 input reproduces `4CCF219942D4180C6DDA3CE36C1B838F` in tests.
- The Python example puts `app_secret` in a URL query. This is not a required common parameter and is not copied; the secret stays local to signing.
- [Common parameters](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=6e2b879dff9c424cbde669bf7100d1b7) specify a seconds timestamp string and JSON. API examples use placeholder `errorCode: 1`; the [error-code guide](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=3420761d5dbd4d409f0e41b67634808a) identifies 1000000 as SUCCESS. Both that code and boolean success are required.
- Product/SKU page-size descriptions say both 100 and 25; this adapter uses the smaller cap pending live certification.

SHA-256 of captured public JSON responses:

| Document | SHA-256 |
| --- | --- |
| Token info | `bd21c794fe695eb257dc111b4d98d7ed209cbad56cc4d478b618acb3fefa2aa0` |
| Products | `fb705ec32abce1a0d3118c561c7f4e5bb963aa8ca66392c97b0d4ef5aec6c12e` |
| SKUs | `eac18a6236d475c99b600a19a57e149e73df64c2a08f2233a20e5f9fee166754` |
| Supply prices | `f4502e1d73f757779ec5bf34fadebb8dceb87ed428b106c23accf75e8fcd935d` |
| Signing | `38804319e37aea6feec58c8efa86d6030139b6d718f1a5a9398d0229c76198fb` |
| Python example | `df92265cdfb8bc484944a3182b152975487393e52c0c7df0cb0421106b1aad6f` |
| Common parameters | `7306ba4aa2a2859514d47f976ae6a9a7f0453c43a773e83b3127e7808e5611af` |
| Error codes | `9870e2e9887eb58fafe11fdedbed833f623869bf1891dc9ef276027447fbb32b` |

## Remaining delivery

Initial seller consent/callback/token exchange, semi-managed catalog APIs, orders
and commercial writes remain unimplemented. Real accounts must verify each region,
seller program, permission, pagination, scope/expiry/error behavior, rate limits
and model analysis. Full-PRD and production acceptance remain open in the
[delivery inventory](prd-delivery-inventory.md).
