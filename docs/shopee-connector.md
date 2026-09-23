# Shopee seller reads and token rotation

Implemented on 2026-09-22. This is a single authorized shop reader with optional
refresh-token rotation. Initial seller authorization/code exchange, orders and
commercial writes remain open. Offline fixtures do not certify a live seller app.

## Official contracts inspected

The public documentation SPA initially returned an empty shell through the search
renderer. Its own publicly referenced `app.e41687f.js` identifies the unauthenticated
`/opservice/api/v1/developer_guide/detail` and `/doc/api/` requests used below.
Ordinary HTTPS requests returned these public documents without cookies or login.
No seller API was called and no third-party SDK was used as protocol authority.

- [API calls and signing](https://open.shopee.cn/developer-guide/16): concatenate
  partner ID, path, Unix seconds, access token and shop ID, with no separator, then
  HMAC-SHA256 using the partner key and lowercase hex. Public token requests omit
  access token and shop ID from the signature. The guide lists three production
  origins: `partner.shopeemobile.com`, `openplatform.shopee.cn`, and
  `openplatform.shopee.com.br`. The adapter accepts only these exact HTTPS origins.
- [Authorization and Authentication](https://open.shopee.cn/developer-guide/20):
  access tokens last up to four hours; refresh tokens last 30 days and are one-use.
  A seller must authorize the app separately. Authorization expiry and access-token
  expiry are different. The shop-info `expire_time` is not an access-token TTL.

| Read / exchange | Public official schema | Response handling |
| --- | --- | --- |
| `GET /api/v2/shop/get_shop_info` | [Shop info](https://open.shopee.cn/opservice/api/v1/doc/api/?version=2&api_name=v2.shop.get_shop_info) | Top-level shop name, region and status; it does not echo shop ID. Identity comes from the exact signed credential-bound shop ID. Linked/affiliate shops are not granted access. |
| `GET /api/v2/product/get_item_list` | [Item list](https://open.shopee.cn/opservice/api/v1/doc/api/?version=2&api_name=v2.product.get_item_list) | Required status values use repeated `item_status` query keys. Follow `has_next_page` and `next_offset`; require forward progress. |
| `GET /api/v2/product/get_item_base_info` | [Base info](https://open.shopee.cn/opservice/api/v1/doc/api/?version=2&api_name=v2.product.get_item_base_info) | Fetch titles/SKUs for at most 50 IDs per batch. Reject duplicate or foreign IDs; preserve missing listed items with `details_available=false`. |
| `GET /api/v2/product/get_model_list` | [Models](https://open.shopee.cn/opservice/api/v1/doc/api/?version=2&api_name=v2.product.get_model_list) | Validate model IDs, tier indexes and options. Keep each SKU's currency, original/current price and reported available/reserved stock. |
| `POST /api/v2/auth/access_token/get` | [Refresh](https://open.shopee.cn/opservice/api/v1/doc/api/?version=2&api_name=v2.public.refresh_access_token) | JSON body contains partner ID, this shop ID and the refresh token. Require returned partner/shop identity and a new token pair; interpret `expire_in` as a duration. |

Documentation inconsistencies are retained as acceptance limits:

- Base-info cURL and parameter examples use `item_id_list=[34001,34002]`; generated
  Java/PHP/Python examples incorrectly/differently show a space-separated array.
  The implementation follows the cURL/parameter JSON-array example. Live acceptance
  must verify this exact encoding; the adapter does not retry alternative encodings.
- Several response examples use `error="-"` as a placeholder. The normative common
  response contract says success requires an empty error string; `"-"` is rejected.
- The initial token example contains an epoch-looking `expire_in`, despite the
  field's duration description. Refresh accepts only an integer duration 1–14400
  seconds, never an inferred absolute timestamp. The initial exchange is not implemented.

SHA-256 of the exact public JSON responses inspected (2026-09-22):

| Document | SHA-256 |
| --- | --- |
| API calls guide 16 | `230eb287f6176074e33a81b5da1730c499549b3fb563cb85ead2f0ecabfecf2e` |
| Authorization guide 20 | `38c8ed8d31a471be5900597c53aaa981e4789feff9239044c84f6558ced1cfd8` |
| Shop info | `c5973ddcb1ff0ffd5bed1af4b88d29a17d9d16b9a50bef78462fda8098b63d6e` |
| Item list | `d11c57c70ea742b4e0a6944838c1b6e62ed5edf6864180d5779597c69996f153` |
| Base info | `5ee65f610570f870f7e667e5b2d8c11e0b6c6dd3282a21779d84fb52ae45e15e` |
| Models | `028d29011fc6bca3df02697f86c868f25eb716d0783a1a6b9947ed3d1b22408b` |
| Refresh | `917ab2578c0dd59ec7ea08dc055e702364882828ca409bb5ab107494054c85a2` |

Guide response URLs use `developer_guide/detail?document_id=16&language_code=en`
and `document_id=20`, respectively, under the same official API prefix above.

## Configure and read

Open **Plugins → Connectors → Shopee → Configure seller access**. Choose the
production API region appropriate for your deployment; enter partner ID, partner
key, exact shop ID, an existing access token and its absolute expiry in local time.
Optionally enter the refresh token and its actual absolute expiry. The inputs do
not start an OAuth redirect or initial token exchange. They are cleared after a
save attempt, including a failed/ambiguous attempt.

Partner/shop IDs are canonical positive decimal **strings** below 2^63 in the
credential bundle. They are not converted to JavaScript numbers. Token expiries
are integer Unix seconds. `OPHIRA_CONNECTOR_SHOPEE_KEY` may supply the same JSON
fields (`partner_id`, `partner_key`, `shop_id`, `access_token`,
`access_token_expires_at`, optional `refresh_token` and `refresh_token_expires_at`).
The private vault takes precedence. `refresh_pending` is internal recovery state,
not a configuration field in the UI.

The application permissions `shopee:shop:read` and `shopee:products:read` are local
read grants, not claims about Shopee OAuth scopes. Saving/health checks establish
configuration only. **Read store** performs the first service request. **Read
products** returns the seller's listings with the selected platform status. **Read
SKU details** fetches an explicit item's models. Prices remain decimal strings,
stock zero remains zero, missing values remain unknown, and currency is taken from
that price record rather than inferred from region. These are not market demand,
competitor sales, inventory forecasts or historical prices.

The shared administrator API accepts:

- `operation=authorized_shops`: only the credential-bound shop.
- `operation=products`, `shop_id`, optional status, page size and returned offset:
  list page size 1–100; base info in batches of 50. UI and Agent use 10 products.
- `operation=product_details`, `shop_id`, `product_id`, page size and optional
  returned offset: `pagination_unit=SKU`, `total_count` is the number of models in
  this response, `variants_offset` locates this slice, and `variants_complete`
  identifies a single complete slice. UI and Agent use 10 SKUs; a caller can follow
  `next_page_token` with the same product ID. The upstream model endpoint itself
  has no pagination, so each page rereads and validates the bounded model response.
  Pages are fresh observations and not an immutable catalog snapshot.

Limits: 30 seconds for the entire operation, 2 MiB per response, 200 models and
three tiers with up to 100 options each. Out-of-range structures fail explicitly.
`partial_data` reports upstream warnings or missing item details without relaying
raw warning text. Every observation retains the fixed source paths, request IDs
for all contributing reads, and retrieval time. Signed URLs, raw error bodies and
credentials are not returned or logged by the shared transport.

**Project access for Agents** grants this verified shop to a saved project with an
expiry. Native `read_marketplace` uses the returned binding ID; it accepts neither
credentials nor arbitrary endpoints. A details observation mints
`marketplace_product_details` receipts with the complete sanitized page and item
ID. The existing 32,000-character source limit remains; oversized fields/pages
fail instead of silently truncating evidence. Evidence recording remains separate.
Existing TikTok Shop, Shopify and SHEIN readers explicitly reject the new detail
operation until their own detail adapters are implemented.

## Refresh and revocation

When an access token has at most 30 seconds remaining, an available refresh token
is used once. A private vault compare-and-swap persists `refresh_pending=true`
**before** network exchange. Concurrent requests cannot reuse that generation,
including across processes. The adapter has no transport retries. A successful
response must match the same partner/shop, supply a different refresh token and a
bounded access-token duration. The new pair replaces only that exact claim.
Expiry is measured conservatively from request start, not response arrival.

Timeout, malformed identity/TTL, cancellation, storage failure or process death
can leave the claim pending. Future reads stop and require operator replacement
of the token pair; they never guess whether the one-use token was consumed.
Environment credentials, if used, become a claimed vault generation before
exchange; the environment is not rewritten. Backups preserve a pending claim.
Restoring an older backup cannot make a token consumed later valid at Shopee;
replace such credentials before use.

Configuration, credential generation and project access are checked around every
network request and before returning the observation. Local revoke disables the
connector and durably suppresses environment fallback. A late refresh cannot
resurrect a deleted key or replace newly configured credentials. Local revoke does
not revoke the app at Shopee; a request already transmitted may finish remotely,
but its result is withheld. Revoke the upstream app separately in Seller Center.

## Verification boundaries

`backend/tests/test_shopee.py` covers the actual adapter, signatures, pinned
transport, malformed/oversized data, precise decimals, missing values, bounded SKU
pages, one-use refresh, environment migration, real two-process contention and
abrupt process death, revoke/replace races, HTTP roles, native SDK MCP and source
receipts. UI tests cover configuration, secret clearing, pagination, unknowns,
project grants, reconnect, busy states and Chinese labels.

These tests use explicit synthetic seller data and controlled transports. No live
seller account, API scope, rate limit, region, OAuth callback, platform write, real
model integration or page visual acceptance is certified by these tests.
