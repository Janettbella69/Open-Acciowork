# ECB reference-rate tool (FR-11)

The main Agent can call `mcp__ophira_fx__get_ecb_fx_reference` with a base and quote
ISO-style three-letter currency code. It reads the [ECB Data API's daily `EXR`
series](https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?lastNObservations=1&format=csvdata&detail=dataonly)
over HTTPS. No key or merchant connector is required. The request uses the shared
public-address DNS pinning rule, verifies TLS for the original hostname, rejects
redirects, and bounds response size and duration.

The output is **quote units for one base unit**. The ECB publishes currency units
per EUR. For USD to CNY, the tool divides the same-day CNY-per-EUR observation by
USD-per-EUR and labels the result as a derived cross rate. The output carries both
basis observations, the observation date, retrieval time, and source URL. Derived
rates are rounded to the least precise source observation (at most 12 significant
digits). The host
adds a source receipt bound to the Run and initialized SDK session; failed reads do
not receive receipts.

The API's `lastNObservations=1` returns one latest row **per currency**, including
historical or suspended series. The tool refuses missing rows, unequal observation
dates, future dates, or observations older than seven calendar days. If the source
is unavailable or malformed, it returns a structured error and no rate. It makes
no fallback to a remembered or model-estimated value.

The [ECB states](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
that its rates are usually published around 16:00 CET on working days, are for
information only, and should not be used for transactions. This tool does not
provide bank settlement, card, merchant, or live execution prices. Other FR-11
supplier, customer, logistics, and licensed data adapters remain separate work.
