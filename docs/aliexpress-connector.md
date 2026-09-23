# AliExpress seller catalog and POP SKU reads

The `aliexpress` connector reads an authorized seller account, status-filtered
product listings and standard POP SKU details. It is separate from the legacy
`alibaba` connector. Implementation is verified with synthetic records and offline
transports; real seller, app-category, regional and model acceptance remain open.

## Configure and read

Open **Plugins → Connectors → AliExpress → Configure seller access**. Enter the
authorized app key/secret, an existing seller access token, the exact `seller_id`
from its authorization response, and the token's expiry in local time. Seller IDs
remain decimal strings, including values larger than JavaScript's safe integer.
Enable product reads only when the app's seller authorization permits them.

Saving proves credential storage only. Select **Read seller account** to verify
that the current profile matches the configured seller ID. This ID identifies a
seller account, not a storefront URL or store ID; the current profile response
does not supply a shop name. The displayed expiry is explicitly the configured
expiry, not a newly queried platform authorization timestamp.

Select a product status, then **Read products**. The six upstream filters are
`onSelling`, `offline`, `auditing`, `editingRequired`, `service_delete` and `deleted`;
there is no combined ALL request. The list response does not independently report
each record's status, so observations label it `status_source: requested_filter`.
Main accounts and subaccounts retain the platform's different catalog visibility.

For a POP product, **Read SKU details** retrieves SKU quotes and inventory. Each
read verifies the seller profile and separately checks that the product belongs
to the token's catalog. Other product types can appear in listings but do not
receive a supported SKU-details button. **Next SKU page** follows the returned
cursor. If product or SKU data changes between pages, reread from the first page;
the application refuses to combine different snapshots.

Project administrators can grant the verified seller account to a saved project
through the existing expiring [marketplace-access contract](marketplace-connectors.md).
The main Agent receives a source receipt for each complete observation. Read data
does not automatically become Evidence or a business commitment.

## Credential and request contract

The vault or `OPHIRA_CONNECTOR_ALIEXPRESS_KEY` holds one JSON object:

```json
{
  "app_key": "AUTHORIZED_APP_KEY",
  "app_secret": "AUTHORIZED_APP_SECRET",
  "access_token": "AUTHORIZED_SELLER_TOKEN",
  "seller_id": "123456789012345678",
  "access_token_expires_at": 1900000000
}
```

`access_token_expires_at` is an absolute Unix timestamp in **seconds**. AliExpress's
authorization response documents `expire_time` in **milliseconds**; divide that
value by 1000 for this JSON field, or enter its actual local date/time in the UI.
The reader does not perform initial consent, code exchange or automatic refresh.
The current [authorization guide](https://open.aliexpress.com/doc/doc.htm#/?docId=989)
distinguishes self-developed apps from service-market ISVs; a universal refresh
policy must not be assumed.

Local permissions are `aliexpress:seller:read` and `aliexpress:products:read`.
These application controls do not confer platform permissions. Every read uses
the fixed HTTPS origin `https://api-sg.aliexpress.com`, path `/sync`, and POST form
parameters `method`, `app_key`, `access_token`, millisecond `timestamp`,
`sign_method=sha256`, `simplify=true`, operation parameters and `sign`.

| Operation | Fixed upstream API | Authority check |
| --- | --- | --- |
| Seller account | `aliexpress.merchant.profile.get` | Returned `profile.seller_id` equals the configured seller |
| Products | `aliexpress.postproduct.redefining.findproductinfolistquery` | Profile recheck and explicit status; structured `aeop_a_e_product_list_query` |
| SKU details | `aliexpress.offer.product.query` | Exact product ID plus a seller catalog query filtered to that ID/status and type POP |

The current [gateway guide](https://open.aliexpress.com/doc/doc.htm#/?docId=636)
and API Java samples identify these as migrated TOP methods. The
[signing guide](https://open.aliexpress.com/doc/doc.htm#/?docId=634) requires sorted
key/value concatenation, including `method` and excluding `sign`, signed with
HMAC-SHA256 and uppercase hexadecimal output. Do not prefix the method as for
native GOP slash paths. A test reproduces the public
[HTTP example's signature](https://open.aliexpress.com/doc/doc.htm#/?docId=633).
The signature guide's “32 characters” comment is inconsistent with SHA-256 and
its working example; the adapter uses the actual 64-character digest.

Credentials travel only in the POST body. Requests pin a validated public DNS
address with the official Host/SNI, disable redirects/proxies/retries, enforce a
2 MiB raw response cap and share a 30-second operation deadline. Credential/vault
generation, expiry and project authority are rechecked before sends and after
responses. Revoke withholds an in-flight response, disables environment fallback
across restarts, and clears the UI's observations; it cannot cancel an already
sent platform request or revoke authorization inside AliExpress.

## Data and pagination semantics

- Product pages use upstream page numbers and at most 50 records (UI/Agent default
  10). Counts belong to the selected status, not the entire seller catalog.
- Detail reads accept at most 256 SKUs and three attribute dimensions, matching
  the [POP integration guide](https://open.aliexpress.com/doc/doc.htm#/?docId=9800).
  SKU pages contain at most the requested count and fit a conservative observation
  budget. Large country-price lists produce fewer whole SKUs per page. No country
  quote is silently truncated. The next cursor advances by the actual returned
  count and binds a digest of the entire normalized product and sorted SKUs.
- Numeric `sku_Id` is a platform ID. When absent, an actual property-combination
  `id` may identify the row, explicitly labeled `id_kind: properties`. The adapter
  does not invent a platform ID or attribute names.
- Listed prices, daily discount prices and per-country discount prices remain
  separate string-valued amounts. They do not establish checkout prices. Missing
  or multi-currency values such as `USD;RUB` remain unknown for a single amount.
  Country pricing configuration is reported as present; its rules are not resolved
  into calculated prices.
- `ipm_sku_stock` is a quantity; `sku_stock` is an independent boolean flag. A flag
  never becomes a fabricated quantity of zero or 999. Missing stock, reserved
  inventory, prices or currency stay unknown.
- The documented simplified examples encode some numbers and booleans as strings.
  The adapter accepts strict canonical strings or the corresponding JSON types,
  rejects duplicate keys/IDs and malformed values, and never reinterprets floats
  as IDs. The product-list example contains both success and a nonzero business
  error; contradictory responses are rejected.
- Returned source requests identify all upstream reads, including membership
  verification. Source paths contain no credentials. External text is sanitized
  and remains untrusted; raw API errors and echoed secrets are not exposed.

## Documentation provenance and verification

Reviewed on 2026-09-22 using the official
[API catalog](https://open.aliexpress.com/doc/api.htm) and its public JSON handlers.
The POP guide was updated 2026-09-02; the gateway/signature guides are dated
2023-10-12. The catalog's older API metadata dates do not establish live compatibility.
No seller credentials were used to retrieve these public documents.

API schemas are available from
`/handler/share/apidoc/getApi.json?cid={category}&path={method}&methodType=GET%2FPOST`
on `open.aliexpress.com`; the profile category is 20895 and product category 20904.
Guide documents use `/handler/share/doc/getDocDetail.json?docId={id}`. SHA-256 of
the public responses captured for this implementation:

| Document | SHA-256 |
| --- | --- |
| Seller profile | `82bbd7af072c73c5e34d14889a4adbce95c2db2ad8ebfbdfa877d70ce6de6110` |
| Product list | `42f623d6b75a4fa47ac427e891fe9629c108e7e22379f26a1c571568816dab14` |
| SKU details | `4efdf182523c27cdd85d2e97e299a382cdf8331bc63f3d276536bba9fb3b025e` |
| HTTP example 633 | `9395082c479526de30b6ae9935c50a520ca0662547a9b07f7fd58da4c8cc8800` |
| Signature 634 | `6e712ade56ce7792a990feb050883e16a7ae73d3ad1008d2a376833043c4dcc7` |
| Parameters 635 | `5cb8e826cb9fd7de10d053543c3f181df988be13f111bcac0b81a02240335923` |
| Gateway 636 | `d77d8da3587c9627638d3caf8786b26e43530f5a2df6d2e982dcb9c17b60cbb9` |
| App authorization 639 | `4e1c2ef286856216013e23437d872c24475a1a017b2d266921db84508b9f97a5` |
| User authorization 989 | `e13ae3e73e6e013b39dc4bd5983531886fb962c7660669177d7fbc6b06589f4b` |
| POP integration 9800 | `d65f32baa7479b642a263fae18e486053e0c597e3eeef3aabe303b4944d5c3e3` |

Offline tests cover signing, seller/product membership, all status filters, full
SKU pagination and snapshot changes, wide country-price pages through native MCP,
unknown values, malformed responses, transport limits, revoke/configuration races,
HTTP authorization, source receipts, credential recovery and the bilingual UI.
Full-suite results are recorded in the [PRD inventory](prd-delivery-inventory.md).

Initial authorization/refresh, semi/full/overseas-managed product adapters, orders
and writes remain undelivered. Real seller/app-category permissions, regions,
rate limits, expiry/error behavior and model analysis require separate acceptance.
The POP guide explicitly excludes managed programs. Browser visual acceptance and
full-PRD production acceptance also remain open.
