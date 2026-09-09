# Marketplace connector contracts

The five developer guides supplied for FR-11/FR-21 are tracked separately. A
developer account, approved app and seller authorization are prerequisites for
private shop data; a documentation link alone does not grant access.

## Platform inventory

Reviewed against official documentation on 2026-09-09. Dynamic documentation
was checked using official indexed pages where the supplied page returned only
its JavaScript shell. These are documentation checks, not authenticated API tests.

| Platform | Official starting point | Delivered | Deferred / reason |
| --- | --- | --- | --- |
| SHEIN | [Developer guide](https://open.sheincorp.com/documents/system/dad0d1c7-be76-4b03-a735-4e23f012bdd9) | Documentation entry and contract investigation | Runtime adapter: full signature rules and applicable seller program still need verification; no authorized account supplied |
| Temu | [Developer guide](https://partner.temu.com/documentation?menu_code=52ef88bdef1d4527b15f6d303b173e48) | Documentation entry and authorization/signature references | Runtime adapter: region-specific gateway and specific read API contracts remain to be verified; no authorized account supplied |
| TikTok Shop | [Developer onboarding](https://partner.tiktokshop.com/docv2/page/developer-onboarding) | Seller credential configuration, authorized-shop reads, selected-shop product pagination, provenance and revoke/reconnect UI; offline adapter/API/UI verification | Live developer/test-shop certification, OAuth callback/token exchange and refresh, Agent/project access binding, receipt-backed evidence import and commercial writes |
| AliExpress | [Register an application](https://developer.alibaba.com/docs/doc.htm?articleId=120674&docType=1&treeId=727) | Correct platform identification and documentation entry | Runtime adapter: verify current app category, read endpoints, permissions and seller authorization; supplied registration page is dated 2022 |
| Shopee | [Developer guide](https://open.shopee.cn/developer-guide/12) | Documentation entry | Runtime adapter: supplied guide was inaccessible in this environment; equivalent global guide returned HTTP 403, so no signature or API contract is assumed |

The Alibaba-hosted link describes **AliExpress**, not Alibaba.com International
or 1688. The existing legacy `alibaba` connector record is preserved; its combined
display name is not used to route AliExpress requests. `tiktok_shop` is a separate
connector type from the existing `tiktok` social account record. No existing
credential is migrated between these platforms.

### Other official references inspected

- SHEIN's [authorization exchange](https://open.sheincorp.com/documents/apidoc/detail/3001520-1000012)
  documents `POST /open-api/auth/get-by-token`, a temporary token, app headers and
  returned seller credentials. This establishes a credential exchange, not the
  complete signing implementation. Its [seller SKC listing](https://open.sheincorp.com/zh/documents/apidoc/detail/3001710)
  is a possible future read contract; it is not implemented here.
- Temu's [signature method](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303&sub_menu_code=4a821c90d06442a09e061b0d4316fbf3)
  describes an MD5-based parameter signature. Its [authorization callback guide](https://partner.temu.com/documentation?menu_code=fb16b05f7a904765aac4af3a24b87d4a)
  distinguishes regional partner portals and seller consent. No generic OAuth
  implementation or gateway is inferred from these pages.
- AliExpress's [endpoint page](https://developer.alibaba.com/docs/doc.htm?articleId=120689&docType=1&treeId=727)
  documents a production gateway, and its [signature page](https://developer.alibaba.com/docs/doc.htm?articleId=120692&docType=1&treeId=727)
  describes HMAC-SHA256 with uppercase hexadecimal output. These older documents
  are investigation inputs; production compatibility has not been certified.

## TikTok Shop read contract

The reader uses fixed host `https://open-api.tiktokglobalshop.com` and only:

| Operation | Official contract | Local minimum permission |
| --- | --- | --- |
| Authorized shops | [GET /authorization/202309/shops](https://partner.tiktokshop.com/docv2/page/get-authorized-shops-202309) | `seller.authorization.info` |
| Products | [POST /product/202502/products/search](https://partner.tiktokshop.com/docv2/page/search-products-202502) | Both `seller.authorization.info` and `seller.product.basic` |

The product operation is a read despite its HTTP POST method. OPHIRA accepts
`shop_id`, page size 1–100 (UI: 20), an optional opaque cursor and a documented
product-status filter. It rereads authorized shops on **every** product request
and resolves the selected shop's cipher on the server. A caller cannot provide a
cipher, URL, arbitrary path or mutation operation. Returned product fields are
limited to ID, title and status; next-page cursors are passed unchanged. The UI
displays a page, not a claim that all products have been retrieved.

The [official signing guide](https://partner.tiktokshop.com/docv2/page/sign-your-api-request)
specifies sorted query values, path and exact non-multipart body bytes, wrapped
with the application secret and signed using HMAC-SHA256. `sign` and
`access_token` are excluded from sorted parameters. Access tokens travel in
`x-tts-access-token`. One body byte string is used for both signing and transport;
URL encoding happens after signing. Timestamps use Unix seconds.

## Configure and use

Open **Plugins → Connectors → TikTok Shop → Configure seller access**. Enter the
developer app key/secret, an existing **seller** access token and its expiry in
local time. Check product permission only when the app's seller authorization
includes it. Save, then select **Read authorized shops**, choose a returned shop,
and select **Read products**. **Next product page** follows only a returned cursor.

The [test-token guide](https://partner.tiktokshop.com/docv2/page/generate-test-access-token)
describes the external token acquisition process. Its `access_token_expire_in`
field is an **absolute Unix expiration timestamp**, despite its name. OPHIRA
stores this as `access_token_expires_at`; it does not add the value to the current
time. Refresh tokens are not collected in this implementation. A token with
30 seconds or less remaining is rejected before a credentialed call.

Alternatively, the operator may set `OPHIRA_CONNECTOR_TIKTOK_SHOP_KEY` to a JSON
bundle with `app_key`, `app_secret`, `access_token`, `access_token_expires_at` and
`user_type: 0`. Use an actual numeric expiry. The connector's configured scope
must also include each required permission. Vault material takes precedence over
the environment. Revocation disables both sources; reconnect requires an explicit
configuration save. Local revocation does not revoke the app at TikTok; a request
already sent may complete remotely, but its response is withheld after local revoke.

Saving credentials proves storage only. A successful shop read confirms that
request at that time; it does not prove product permission or future availability.
The platform remains authoritative for actual grants. A successful response
contains retrieval time, official source path without query, upstream request ID
when supplied, and `data_scope: authorized_seller_data`.

## Boundaries and verification

- `GET /api/connectors/marketplaces` exposes only five official guide entries and
  implementation availability. Four planned adapters cannot be invoked.
- `POST /api/connectors/{id}/marketplace-read` accepts only the two typed read
  operations. In trusted-gateway mode it requires an owner/admin, independently
  of the shared connector mutation guard. Local-only mode retains the existing
  loopback/source checks. It is not exposed as an Agent tool.
- Credentials stay in the existing server vault or connector-only environment
  namespace. Invalid credential input is not echoed in validation responses.
  Shop ciphers, access tokens, app secrets and signed URLs are not returned or
  placed in project artifacts. Results have `Cache-Control: no-store`.
- DNS addresses must be public unicast. Transport connects to a validated numeric
  address while retaining official Host, SNI and TLS certificate validation.
  There are no proxies, cookies, redirects or automatic retries. Public HTTPX
  transport avoids its higher-level signed-URL INFO log. Each response has a 2 MiB
  raw byte cap; the entire operation has a 30-second deadline. Responses must be
  uncompressed JSON; requests ask for identity encoding.
- Configuration, revoke state and token expiry are checked before network sends
  and before results are released. Data is normalized to bounded fields, external
  instruction boundaries are sanitized, and reflected private values are removed
  or rejected. Error bodies and transport exception text are withheld.
- These are private seller/shop observations, not platform-wide demand,
  competitor sales or supplier-market coverage. Reads do not create evidence,
  project truth, scheduled commitments, or modify marketplace products.

Offline tests exercise deterministic signature vectors, exact transmitted bytes,
header-only tokens, DNS pinning, cursor round trips, private-address rejection,
scope/expiry checks, mid-request rotation/revoke/expiry, response bounds and closure,
safe errors, production FastAPI role checks and frontend configuration/pagination.
They do **not** certify a real TikTok app, test shop, live grant, region, rate limit
or OAuth flow. Browser visual QA remains deferred because the in-app Browser is
unavailable in this session. No real credentials or marketplace requests were used.
