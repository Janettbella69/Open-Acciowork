# SHEIN authorization and seller reads

SHEIN now uses the existing marketplace API, expiring project shop grants, native
SDK main-Agent tool and server source receipts. This reads an authorized seller's
store, product catalog and SKU quotes, with optional inventory reads. It does not
measure marketplace demand.

## Authorize a seller

Open **Plugins → Connectors → SHEIN → Authorize through SHEIN**. Enter the
approved app ID, select its official API origin and choose store-only or store
and product access. Generate the link, continue to SHEIN, sign in with the
seller's main account and approve. On return, enter that app's secret and select
**Verify and save SHEIN connection**. OPHIRA exchanges the temporary token,
decrypts the returned seller key, verifies the seller's store identity and then
saves the connection. Project shop grants remain a separate human action.

The app secret is used for this exchange and decryption; it is not saved.
The callback's `tempToken` expires ten minutes after SHEIN approval. OPHIRA's
pending authorization expires thirty minutes after link generation. New links
cancel earlier pending or exchanging flows for that connector. Opening the
callback performs a status check only; exchanging credentials requires the
explicit button. A failed or interrupted exchange is not automatically retried.

The two allowed production origins are `https://openapi.sheincorp.com` and
`https://openapi.sheincorp.cn`. Select the origin assigned to the app's seller
program: the official guide assigns traditional/self-operated and semi-managed
apps to `.com`, and its other listed programs to `.cn`. Domain names do not
establish data residency. Test-environment hosts are not supported here.

### Deployment and callback

- Add the exact frontend origin to `OPHIRA_CORS_ORIGINS`; HTTPS is required except
  for loopback HTTP during local development. Wildcards and caller-selected
  redirect paths are rejected.
- Serve the frontend SPA at `/app/connectors/shein-authorize/{connector_id}`.
  A reverse proxy must preserve this route and the authenticated gateway session.
  The API callback state is bound to the initiating administrator, tenant, role
  and session. Fresh request assertions within that session are accepted.
- Configure the frontend host and reverse proxy to omit callback query strings
  from access logs. SHEIN sends the temporary token in the callback URL; client
  code cannot erase logs produced before the page loads. The HTML sets
  `Referrer-Policy: no-referrer` via its meta policy, and initialization removes
  query/fragment parameters from browser history before React makes API calls.
  Tokens remain in page memory, not browser storage.
- Keep the callback tab open until completion. A full page reload discards its
  in-memory callback; return to Connectors and generate a new link if necessary.
  After a lost response, use **Check authorization status** in the existing tab.
  **Recover saved connection** completes an interrupted local installation within
  the flow's validity period without sending the token or app secret again.

The administrator API uses POST and `Cache-Control: no-store`, including error
responses. Paths share `/api/connectors/{connector_id}/shein-authorization/`:

| Suffix | Body | Result |
| --- | --- | --- |
| `start` | `app_id`, fixed `endpoint`, `allow_products`, `redirect_origin` | Official authorization URL and expiry; existing connection remains unchanged |
| `status` | `state` | App, intended origin/permissions, status, expiry and verified supplier ID if available |
| `finish` | `state`, `app_id`, `app_secret`, `temp_token` | Verified saved connector; only `state` is needed to recover an already saved credential |
| `cancel` | `state` | Cancels pending/exchanging authorization; saved connections are revoked separately |

## Existing seller keys

Open **Plugins → Connectors → SHEIN → Configure seller access**. Select the official
API origin used by the seller integration, enter its `openKeyId` and **already
decrypted seller `secretKey`**, choose whether to allow products, and save. Both
fields are masked and cleared after submission. Saving confirms storage only;
select **Read store** to check that request, then **Read products**.

This manual form accepts an existing decrypted seller key, not the encrypted
`secretKey` returned by the authorization API. Use **Authorize through SHEIN**
for the exchange/decryption flow above.

An operator can alternatively supply `OPHIRA_CONNECTOR_SHEIN_KEY` as a JSON
object containing `open_key_id` and `secret_key`. Vault credentials take precedence.
The configured permissions `shein:store:read` and `shein:products:read` control
OPHIRA access; they are not SHEIN OAuth scope names or proof of upstream permission.
Product and project reads require both. Revocation blocks local use; it does not
revoke the external integration at SHEIN.

## Verified authorization contract

Primary documentation reviewed on 2026-09-22:

- [Authorization manual](https://open.sheincorp.com/documents/system/2169474d-1d4a-41a9-b9fd-427f63f54a63):
  `https://openapi-sem.sheincorp.com/#/empower`, with app ID, Base64-encoded UTF-8
  callback and random state. Callback fields are `appid`, `tempToken`, `state`.
- [Authorization exchange, 3001520](https://open.sheincorp.com/documents/apidoc/detail/3001520-1000012):
  POST `/open-api/auth/get-by-token`, body `{"tempToken": "…"}`, app headers
  `x-lt-appid`, `x-lt-timestamp`, `x-lt-signature`. The existing signature contract
  below uses app ID/app secret for this request. Success requires code `"0"`,
  the same app/state and a valid integer supplier ID.
- The manual specifies Base64 ciphertext, AES-128-CBC, the first sixteen UTF-8
  bytes of the app secret as key, the first sixteen UTF-8 bytes of
  `space-station-default-iv` as IV and PKCS7 padding. A test independently
  decrypts its published sample to `Hello World`. The decrypted key must then
  retrieve the same supplier via the store API before installation.

The official portal's public document handlers supplied the full manual and
schema; no authenticated seller request was needed. Reproduction endpoints:
POST `/api/page/detail?pageId=2169474d-1d4a-41a9-b9fd-427f63f54a63` with `{}`, and
GET `/api/api/apiDoc/queryApiPublishDocDetailInfoById?id=3001520&isLatest=true` on
`https://open.sheincorp.com`. Retrieved response SHA-256 values were respectively
`996fbd85c48b27ee7cf66b8a973dcd0147a7a4e20ad88dd04d347a07303e613e` and
`0573fbc9da2277d9d3566b923b8fa1a143dcad070b22d1aff6a5d38c57f3eaff`.
Some legacy examples still reference a test host; the newer production examples,
fixed production origins and manual agree on the contract used here.

## Verified read contract

Primary documentation reviewed on 2026-09-22:

| Operation | Official source | Request and normalized response |
| --- | --- | --- |
| Store | [Store information, 3001499](https://open.sheincorp.com/pt/documents/apidoc/detail/3001499) | POST `/open-api/openapi-business-backend/query-store-info` with `{}`; supplier ID, optional name and business mode. Missing region stays unknown. |
| Products | [Product search, 3001634](https://open.sheincorp.com/zh/documents/apidoc/detail/3001634), latest schema 3001938, updated 2026-08-03 | POST `/open-api/goods/searchProduct`; `pageNum` starts at 1, `pageSize` is 1–10, `languageList: ["en"]`. Optional `skcShelfStatus` is 0/1; SKU reads use `spuNameList` to verify exact seller catalog membership. |
| SKU details | [Product details, 3001811](https://open.sheincorp.com/zh/documents/apidoc/detail/3001811), latest schema 3002017, updated 2026-09-02 | POST `/open-api/goods/spu-info` with `spuName` and `languageList: ["en"]`; published and platform-approved products only. SPU → SKC → SKU, site quotes, supply prices, sales/purchase states and sales attributes. |
| Inventory | [Stock query, 3001695](https://open.sheincorp.com/pt/documents/apidoc/detail/3001695-1000003), updated 2026-04-27 | POST `/open-api/stock/stock-query` with one `spuNameList` and explicit `invType`; SKU totals and per-warehouse quantities. This is distinct from the SHEIN-owned `/open-api/sims/stock-query` program. |

Products remain **SPU groups with nested SKC variants**. The response retains SPU
ID/status/category, SKC ID/status/English title, page number, reported total SPU
count and an optional next-page token. No SPU title is invented from a variant.
UI and Agent requests use at most 10 groups; variant counts never determine the
next page. Changing the shelf filter clears the previous page.

### Read SKU prices and inventory

After **Read store → Read products**, choose **Inventory to read with SKU details**
and select a product's **Read SKU details** action. The default reads prices only.
Select the inventory that applies to the seller's program when stock is needed:

| Selection / `inventory_type` | Official `invType` meaning |
| --- | --- |
| Omitted | Prices only; no inventory request |
| `PI` | SHEIN physical warehouse inventory |
| `VI` | Merchant virtual inventory |
| `JI` | Merchant JIT inventory |

Example administrator request to `POST /api/connectors/shein/marketplace-read`:

```json
{
  "operation": "product_details",
  "shop_id": "21871704",
  "product_id": "spu_example",
  "page_size": 10,
  "inventory_type": "VI"
}
```

The IDs above are examples; use IDs from the authorized store/catalog response.
The native main-Agent MCP uses a current project `binding_id` in place of the
administrator's `shop_id`. Existing store and product permissions govern these
reads, and SHEIN must separately authorize the upstream APIs.

Each detail read verifies the current store, checks the exact SPU in that seller's
catalog, fetches its details, and optionally fetches inventory. Returned SPU/SKC/SKU
identities must agree throughout. A quote-only page records three source requests;
an inventory page records four. The final source is the details or stock endpoint,
and `source_requests` retains every endpoint and its reported request ID.

`site_prices` retains site, currency, `base_price` and `special_price` independently.
`supply_prices` retains currency and cost-price amount; it is not retail pricing.
The detail schema still reports both base/special fields and its example includes
a zero special price. Neither a zero nor the presence of these quotes establishes
an effective checkout price. Decimal JSON values become exact decimal strings;
missing currency or amounts stay null. Attribute IDs remain exact strings, including
int64 values beyond JavaScript's integer precision. Missing English labels stay blank.

Inventory retains `total`, `locked`, `temporary_locked`, `usable`, `in_transit`
and `stockout_demand` for SKU totals and for each warehouse. The reported warehouse
type is preserved without guessing a mapping. Totals and warehouse rows are not
added together. Missing quantities remain null rather than becoming zero; absent
SKU records remain `inventory: null`. `partial_data` flags omitted product/SKU
records, required stock quantities or the warehouse list. Optional quantities may
be unknown even when the flag is false.

**Next SKU page** counts SKU rows independently of SPU catalog pages. The cursor
contains the consumed SKU offset and a digest of the full normalized product,
shop and selected inventory type. Each page repeats all reads; a changed product,
quote or stock observation returns `MARKETPLACE_PAGINATION_CHANGED` (409), requiring
a fresh first page. Catalog `updateTime` is not used because its documented update
triggers exclude price and inventory changes. This detects inconsistent pagination;
it does not make the platform's separate requests an atomic transaction.

The administrator API accepts 1–100 requested SKUs; the UI/native Agent use up to
10. Adaptive pagination keeps complete SKU rows within the existing MCP envelope;
large individual rows fail explicitly rather than being truncated. Local bounds
include 100 SKCs per SPU, 400 SKUs per SKC, 4,000 SKUs per SPU, 100 site quotes,
100 supply quotes and 100 warehouses per SKU. These are implementation limits,
not claims about every seller program's catalog maximum.

### Stock schema discrepancies and reproducibility

The stock documentation recommends `invType` and schedules the old request
`warehouseType` for removal on 2026-12-31, but its field schema still marks
`warehouseType` required. This adapter uses the documented replacement `invType`
only; it does not guess an old seller-program code or retry with another type.
Live acceptance of this migration remains unverified. Old numeric warehouse types
are not interchangeable with PI/VI/JI.

The stock schema describes `info` as an object, while its current official example
uses an array of objects. Only these two documented shapes are accepted. Warehouse
`outOfStockQty` is documented as a string and is validated as a nonnegative integer;
other quantities must be integer JSON numbers. Duplicate identities/quote keys,
fractional or invalid counts and oversized structures fail closed.

The public portal handler
`GET /api/api/apiDoc/queryApiPublishDocDetailInfoById?id={id}&isLatest=true`
returned the latest schemas for requested IDs 3001634, 3001811 and 3001695.
SHA-256 values of the retrieved JSON, respectively:

- Catalog: `f3f90e8724fbdf6c62301bc65ff2352c5396d4e729581a41ca9675a04a75100d`.
- Details: `33c9cd1cd8f1db2efd6fcdc38c7e98057fb4a1742c530aa65d94f7cb891260ac`.
- Stock: `de4d6a39aab80619baf3478d8b285d3cb52d4c4a2046aab0104622dfaaffdf32`.

### Request signing

The [official signature tool](https://open.sheincorp.com/zh/documents/system/passwdrule)
signs `openKeyId&timestamp&path` using HMAC-SHA256 with key
`secretKey + randomKey`. Timestamp is Unix milliseconds; randomKey is five
ASCII letters/digits. The signature prefixes randomKey to Base64 of the lowercase
hex digest's UTF-8 bytes, not Base64 of the raw digest. Credentials/signature use
`x-lt-openKeyId`, `x-lt-timestamp`, `x-lt-signature` headers.

Evidence includes the signature page's own public
[JavaScript asset](https://assets.dotfashion.cn/webassets/openapi-portal-front/43867/38724/_next/static/chunks/app/%5Blocale%5D/(documents)/documents/system/passwdrule/page-2afc0f6b93b325ef.js),
SHA-256 `ed12dc951b552e017853d9ac962db92bc19a2d098ea974c012a76dff92a18a11`.
The official API page's public
[debugger asset](https://assets.dotfashion.cn/webassets/openapi-portal-front/43867/38724/_next/static/chunks/app/%5Blocale%5D/(documents)/documents/apidoc/detail/%5Bid%5D/page-4c2da50387bd7b86.js)
confirms both API origins; SHA-256
`de3753a8619701608f38eacfa3550b2373e3f16b5b7d3a1c8a8481328e08646e`.

## Authorization and verification boundaries

Before the exchange, a durable claim prevents duplicate attempts across requests
and process restarts. A timeout leaves its outcome uncertain and requires new
consent; the same token is never replayed. Flow records contain only a hash of
state, an actor binding, connector snapshots, credential generation and progress.
They contain no app secret, temporary token or seller secret. Records are bounded
to 1,000 and entries expired for over thirty days are pruned on the next start.

Installation serializes connector publication and revocation across processes.
Saved seller credentials bind the authorization ID, API origin, local permission
scope and supplier ID. Interrupted metadata publication cannot use those keys
under the old origin/permissions; recovery uses their saved marker. Credential
deletion, rotation, configuration changes or cancellation prevent a stale flow
from replacing the current connection. Cancellation does not revoke an approval
already granted at SHEIN.

Each product read verifies the seller's supplier ID again before querying products.
Permissions, connector configuration and Vault credential generation are checked
around network waits, including revoke/reconnect with the same key. Requests already
sent may finish remotely; a revoked response is withheld. The existing public-DNS
pinning, fixed paths, no redirects/retries, 2 MiB response cap and 30-second operation
deadline apply. Signed headers, raw upstream errors and reflected credentials are
not returned or logged.

**Project access for Agents** grants one verified shop for a fixed duration.
The main Agent receives the same SPU/SKC or SKU observation and a source receipt; creating
Evidence remains a separate action. A lost save/revoke response locks dependent UI
actions until **Refresh connector status** obtains current server state.

Offline tests cover the published AES sample, independently calculated signatures, exact wire
requests, pagination, malformed/oversized data, credential rotation/revocation
during every network wait, authorized FastAPI routes, actual native SDK MCP dispatch,
receipt integrity, exchange claims, actual process exits during claim/publication,
interrupted installation recovery, callback parameter removal and English/Chinese
interactions. SKU tests also cover all inventory types, exact decimal values,
unknown quantities, identity mismatches, changed-snapshot rejection and complete
pagination of wide quotes/warehouse rows through actual native MCP dispatch.
No live seller/model request
was made. Browser discovery returned no available browser, so visual QA remains
open. Live seller-program authorization and SKU/stock-schema compatibility, rate
limits, orders and commercial writes remain deferred.
