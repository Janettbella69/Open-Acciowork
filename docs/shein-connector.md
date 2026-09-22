# SHEIN seller reader

SHEIN now uses the existing marketplace API, expiring project shop grants, native
SDK main-Agent tool and server source receipts. This reads an authorized seller's
store and product catalog. It does not measure marketplace demand.

## Configuration

Open **Plugins → Connectors → SHEIN → Configure seller access**. Select the official
API origin used by the seller integration, enter its `openKeyId` and **already
decrypted seller `secretKey`**, choose whether to allow products, and save. Both
fields are masked and cleared after submission. Saving confirms storage only;
select **Read store** to check that request, then **Read products**.

The [authorization exchange](https://open.sheincorp.com/documents/apidoc/detail/3001520-1000012)
returns an encrypted secret. It must be decrypted by the authorized integration
before configuration here. OPHIRA does not collect an app secret, implement that
exchange/decryption or infer that a syntactically valid key is decrypted.
The two allowed origins are `https://openapi.sheincorp.com` and
`https://openapi.sheincorp.cn`; these domain names do not establish data residency.

An operator can alternatively supply `OPHIRA_CONNECTOR_SHEIN_KEY` as a JSON
object containing `open_key_id` and `secret_key`. Vault credentials take precedence.
The configured permissions `shein:store:read` and `shein:products:read` control
OPHIRA access; they are not SHEIN OAuth scope names or proof of upstream permission.
Product and project reads require both. Revocation blocks local use; it does not
revoke the external integration at SHEIN.

## Verified read contract

Primary documentation reviewed on 2026-09-22:

| Operation | Official source | Request and normalized response |
| --- | --- | --- |
| Store | [Store information, 3001499](https://open.sheincorp.com/pt/documents/apidoc/detail/3001499) | POST `/open-api/openapi-business-backend/query-store-info` with `{}`; supplier ID, optional name and business mode. Missing region stays unknown. |
| Products | [Product search, 3001634](https://open.sheincorp.com/zh/documents/apidoc/detail/3001634) | POST `/open-api/goods/searchProduct`; `pageNum` starts at 1, `pageSize` is 1–10, `languageList: ["en"]`. Optional `skcShelfStatus` is 0/1. |

Products remain **SPU groups with nested SKC variants**. The response retains SPU
ID/status/category, SKC ID/status/English title, page number, reported total SPU
count and an optional next-page token. No SPU title is invented from a variant.
UI and Agent requests use at most 10 groups; variant counts never determine the
next page. Changing the shelf filter clears the previous page.

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

Each product read verifies the seller's supplier ID again before querying products.
Permissions, connector configuration and Vault credential generation are checked
around network waits, including revoke/reconnect with the same key. Requests already
sent may finish remotely; a revoked response is withheld. The existing public-DNS
pinning, fixed paths, no redirects/retries, 2 MiB response cap and 30-second operation
deadline apply. Signed headers, raw upstream errors and reflected credentials are
not returned or logged.

**Project access for Agents** grants one verified shop for a fixed duration.
The main Agent receives the same SPU/SKC observation and a source receipt; creating
Evidence remains a separate action. A lost save/revoke response locks dependent UI
actions until **Refresh connector status** obtains current server state.

Offline tests cover an independently calculated signature vector, exact wire
requests, pagination, malformed/oversized data, credential rotation/revocation
during both requests, authorized FastAPI routes, actual native SDK MCP dispatch,
receipt integrity and English/Chinese interactions. No live seller/model request
was made. Browser discovery returned no available browser, so visual QA remains
open. Live seller-program compatibility, rate limits, OAuth/decryption, SKU-level
price/inventory, orders and commercial writes remain deferred.
