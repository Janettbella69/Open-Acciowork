# World Bank market indicators through the Web MCP (FR-11)

The main Agent can use the existing `mcp__ophira_web__fetch_page_snapshot` tool to read a bounded [World Bank Indicators API](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581) request. A reviewed WDI URL receives a structured projection while all other URLs keep the ordinary text snapshot behavior. This adds no MCP tool to the Provider prompt.

Example for Germany's recent total merchandise imports:

```text
https://api.worldbank.org/v2/country/DEU/indicator/TM.VAL.MRCH.CD.WT?format=json&source=2&mrv=5&per_page=5
```

The path needs an ISO3 country code and one of `TM.VAL.MRCH.CD.WT` (total merchandise imports, current US$), `NE.CON.PRVT.CD` (household final consumption, current US$), or `SP.POP.TOTL` (population, persons). The query must use JSON, WDI source 2, and one to five recent periods on a single page. The adapter checks every returned row's country, indicator, year and finite nonnegative value. Missing values remain `null`; malformed, redirected, oversized or cross-country responses produce a structured error. Generic public URL safety still pins DNS and verifies the original TLS hostname.

Successful results include `source_url`, `source_id`, `retrieved_at`, upstream `source_updated_at` when supplied, `applicable_market`, and dated observations. The existing host hook attaches a source receipt scoped to the Run and SDK session; the Agent cites its ID when recording Evidence. The [market-research Skill](../backend/ophira-capabilities/skills/market-research/SKILL.md) explains when to use this source.

These annual country aggregates are macro context. They do not measure the demand or sales for any product, identify buyers or suppliers, or forecast opportunity. `retrieved_at` is the API read time, and `source_updated_at` is not the year of each observation. Check the [indicator metadata](https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries), revisions, upstream sources and [terms](https://data.worldbank.org/summary-terms-of-use) before using figures externally; cite World Bank, World Development Indicators with indicator, country, observation year and access date.

Offline mocked API and receipt tests cover the contract. The local network currently cannot resolve `api.worldbank.org`, so a live read and a real Provider Run remain unverified. This path does not complete FR-11's supplier, customer, product-level market, compliance or logistics adapters.
