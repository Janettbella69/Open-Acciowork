# TradeMind

**跨境电商版的 Codex，对标阿里的 Accio Work**

> 发现世界所需，连接能够供应的人。  
> *Discover what the world needs. Connect who can supply it.*

## 产品定位

TradeMind 连接全球需求、商品、供应链和客户，帮助跨境团队从市场机会走向真实成交。

## 我们连接什么

- 全球需求
- 商品与供给
- 供应链
- 客户与成交机会

## 核心品牌主张

**发现世界所需，连接能够供应的人。**

**Discover what the world needs. Connect who can supply it.**

## 名字背后的含义

TradeMind 由两个最简单的英语词组成：Trade 表达跨境贸易，Mind 表达能够研究、判断并推动下一步的 AI 商业大脑。

它践行的商业理念是：让需求被看见，让供给被组织，让价值跨越边界，抵达真正需要它的人。

## Frontend

The repository now contains two connected React surfaces:

- `/` — the TradeMind marketing site and product story.
- `/app` — the interactive AI global commerce workspace.

The product rail also opens Agents (`/app/agents`), a Plugins hub (`/app/plugins`)
with Skills and Connectors sections, Scheduled tasks (`/app/schedules`), and Mobile
trusted-device pairing (`/app/mobile`). Companion access is disabled until a
same-origin HTTPS deployment is configured; optional Web Push additionally requires
operator VAPID configuration and explicit browser subscription.
See [`docs/mobile-companion.md`](docs/mobile-companion.md).

Human approvals now persist through native SDK deferral and resume the original
Run across control-plane workers; see [`docs/durable-approvals.md`](docs/durable-approvals.md).
Scheduled runs can retain scoped, traceable task experience without changing user
policy or global memory; see [`docs/task-experience.md`](docs/task-experience.md).
Manual and automatic schedule triggers now share cross-process overlap checks;
see [`docs/schedule-overlap.md`](docs/schedule-overlap.md).
Run-bound evidence, decisions, artifact versions and memory proposals emit recoverable
ledger observations; open **Run memories** in a workspace to review proposals from that task. the
workspace drains the complete Run event tail before closing its stream.
See [`docs/project-events.md`](docs/project-events.md).

Shared projects now include member management and durable discussions with replies,
source/version anchors, edit history, and conflict checks. See
[`docs/project-collaboration.md`](docs/project-collaboration.md) for role boundaries
and local versus organization sign-in behavior.

Open **Project templates** to save a reviewed project brief and fixed reference-file
versions, inspect history, and create a separate project with customized settings.
Interrupted creations retain reviewable recovery records. See
[`docs/project-templates.md`](docs/project-templates.md) for ownership, copied material
and cold-backup boundaries.

Tenant Skills keep immutable, content-addressed package history with per-run snapshots,
version inspection, reviewed rollback, bounded storage, and active-session-safe runtime
leases. Workspace results can also render typed evidence, decision, and artifact cards
whose factual fields are joined from the server ledger rather than authored by the model.

**Skills → Private repository** adds administrator ZIP import review, frozen file
inspection, disabled publication with durable receipts, and separately confirmed
enablement or version restoration. Organization members can inspect and download
published packages. See [private Skill repository](docs/private-skill-repository.md)
for API changes, single-deployment scope and recovery limits.

All 13 built-in Skills include evidence guides and editable artifact templates that
travel with the immutable package snapshot; see [`docs/skill-resources.md`](docs/skill-resources.md).
The local [Skill backup and restore exercise](docs/skill-backup-exercise.md) verifies
independent copies, historical pins, disabled versions, and rejected corrupt restores.
The operator [application cold backup](docs/application-backup.md) captures the complete
data directory with its Plugin, verifies project/Run/proposal/receipt links, and restores
into a new directory with reviewed publication recovery. Stop the application before
backup; restoration does not start jobs or model sessions.
**Settings → Backup and recovery** explains how to open the separate local maintenance
interface after shutdown. It reviews a complete backup or restored copy, shows its contents
and destination, and requires confirmation before publication. Durable requests survive
interrupted responses and process exits. The operator activates restored data separately;
see [the maintenance interface](docs/application-backup.md#本机维护界面).
**Settings → Providers → Credential backups and deletion** exports individual Provider
or connector credentials, plus Feishu signing/application and Web Push private keys,
in password-encrypted files and requires explicit confirmation
to delete the reviewed key. Deletion also disables environment fallback across restarts.
Application service keys can be explicitly replaced from the same panel; running adapters
observe their current generation and discard revoked caches before new requests.
See [credential management](docs/credential-management.md) for offline recovery,
administrator access and the boundary with external revocation and older backups.
Deployment-owner gateway signing keys have a separate [local maintenance command](docs/deployment-credentials.md)
for encrypted export, reviewed deletion, replacement and recovery. Mutations require all
application workers to be stopped; ordinary HTTP administrators cannot access these keys.
Open **Export project data** in a saved workspace to review and download a project
archive or the current conversation, including available registered file versions
and project memory history. Downloads require current owner/admin access and
finished runs; the ZIP is unencrypted. See [project data export](docs/project-data-export.md)
for included records, excluded shared data and the distinction from app recovery.
**Review conversation deletion** removes the conversation and attributable context while
keeping saved project outputs. **Review project deletion** removes the complete project,
file history, related automation, shop logins and project access after reviewing the scope,
typing its name and confirming. Independent template copies remain usable. Interrupted
commits recover before ordinary data access resumes; retries cannot recreate deleted IDs.
See [project and conversation deletion](docs/project-data-deletion.md) for authorization,
retained provenance and the boundaries with shared content, external services and old backups.
The commerce MCP also calculates cash balances and peak funding gaps from explicit
settlement schedules, distinguishing daily closing balances from a conservative
intraday bound; see [`docs/cashflow.md`](docs/cashflow.md).

**Plugins → Connectors** now includes official onboarding links for SHEIN, Temu,
TikTok Shop, AliExpress, Shopee and Shopify. TikTok Shop has a seller-authorized shop and
product reader with pagination, scoped credentials and revoke/reconnect controls.
Shopify reads one store in the app's Dev Dashboard organization through the client
credentials grant (24-hour tokens kept in memory): shop details, products and recent
orders without customer identity, guided by the `shopify-store-review` Plugin Skill.
SHEIN provides seller consent, one-attempt token exchange, key decryption and
verified connection saving with interrupted-installation recovery. It reads the
authorized store and paginated SPU groups with nested SKC variants; existing
decrypted seller keys are also supported. See [SHEIN setup](docs/shein-connector.md).
Project administrators can grant one shop for a fixed duration; the main Agent
can then read that shop and cite server-issued source receipts when recording evidence.
Shopee reads single-shop products and SKU prices/stock with optional durable token refresh.
Temu reads local-seller products/SKUs and separately authorized supply prices through
regional gateways; see [Temu setup](docs/temu-connector.md).
AliExpress verifies a seller account and reads status-filtered products and POP SKU
quotes/stock, with complete, snapshot-bound SKU pages; see [AliExpress setup](docs/aliexpress-connector.md).
Other platforms' initial authorization flows and live seller certification remain open; see
[`docs/marketplace-connectors.md`](docs/marketplace-connectors.md).

Open **Project run metrics** in a saved workspace to inspect recent execution,
latency, observed cost estimates, and Provider/Skill/tool groups. Missing data stays
explicit; see [`docs/project-metrics.md`](docs/project-metrics.md).

Deep Research runs also show frozen budget/deadline limits, native research-unit
activity and reported task usage. See [`docs/deep-research-resources.md`](docs/deep-research-resources.md)
for the source-merge contract and the distinction between SDK estimates and billing.

Open **Project knowledge** in a saved workspace to search registered text files,
OCR Markdown and DOCX/XLSX/PPTX text, inspect paragraph/cell/slide locations and
version provenance, and download a freshly hash-checked source. Office extraction
limits and coverage gaps stay visible; matches are reference material and
do not automatically become verified evidence. See
[`docs/project-knowledge.md`](docs/project-knowledge.md).

Open **Project files** to download exact saved versions, inspect their history,
and preview/edit text with conflict checks. New registrations preserve immutable
bytes; user edits retain the prior version and become searchable project files.
See [`docs/artifact-versions.md`](docs/artifact-versions.md) for supported formats,
interrupted-edit recovery and the availability boundary for legacy versions.

Open **Workspace recovery** to review and restore a complete supported file tree
saved before a Run, including files changed by Bash. Recovery preserves the current
workspace as another recovery point and retains project truth and version history.
See [`docs/workspace-recovery.md`](docs/workspace-recovery.md) for the review flow,
interrupted directory swaps, concurrent-run protection and capacity limits.

PDF/image uploads produce bounded Markdown companions with honest extraction
diagnostics; optional local PaddleOCR-VL setup and remaining quality checks are in
[`docs/ocr-ingestion.md`](docs/ocr-ingestion.md). Result/evidence feedback now has
exact ledger anchors, human review and explicit unlabeled evaluation-candidate
export; see [`docs/feedback-loop.md`](docs/feedback-loop.md).

### Run locally

Start the backend from the repository root:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e './backend[dev]'
cp backend/.env.example backend/.env
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload
```

To enable the Playwright browser ladder, install its optional dependency and
managed Chromium binary once:

```bash
.venv/bin/pip install -e './backend[dev,browser]'
.venv/bin/playwright install chromium
```

Set an Anthropic API key in `backend/.env`, or pair `ANTHROPIC_BASE_URL` with the
provider credential and model for an Anthropic-compatible gateway. DeepSeek, Kimi,
GLM, and Qwen examples are documented in [`docs/model-providers.md`](docs/model-providers.md).
The backend passes this configuration directly to Claude Code; it does not translate
requests through an OpenAI-compatible API. Then start the frontend in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173/app`. Vite proxies `/api` and the SSE stream to `http://127.0.0.1:8000`. For separate deployments, copy `frontend/.env.example` and set `VITE_OPHIRA_API_URL` to the backend origin.

### Verify

```bash
# backend（若刚移动/重命名过仓库，先 python3.11 -m venv .venv --clear 重建 venv）
.venv/bin/ruff check backend/src backend/tests
PYTHONPATH=backend/src .venv/bin/pytest backend/tests

# frontend
cd frontend
npm run test:run
npm run lint
npm run build
```

### Current verification boundary

The full PRD delivery inventory, including unimplemented and externally unverified
requirements, is tracked in [`docs/prd-delivery-inventory.md`](docs/prd-delivery-inventory.md).

Saved projects also provide **Project shops**: configure a shop identity and optional
platform Connector, open its independent login browser, confirm the signed-in shop,
then let the main Agent collect source-linked page captures and reports. The window
runs on the backend computer. See [setup, grants and verification](docs/shop-browsers.md).

- The native durable-approval path passed three consecutive real DeepSeek Green Path
  rounds on 2026-09-05 (local time), archived at
  `evals/reports/greenpath-20260906T044246Z/`. This includes same-Run deferred tools,
  denial followed by continuation, exact SSE replay, and conversation resume.

- The current nine-case DeepSeek Provider contract matrix was rerun against the real endpoint on
  2026-09-04 with Claude Agent SDK 0.2.143: text streaming, served-model identity,
  file tools, Bash, MCP, Plugin Skill discovery, and native WebSearch passed. Vision
  remains unavailable and invalid credentials still rely on the bounded preflight
  timeout instead of an SDK fast failure.
- The archived real-endpoint bundle `evals/reports/greenpath-20260904T082202Z/`
  passed three consecutive DeepSeek Green Path rounds with actual Plugin Skill invocation,
  Read/Web/MCP/Bash/Write/Edit coverage, same-run source-receipt Evidence, permission
  deny-then-continue, SSE replay, and session resume. It does not prove a real commercial
  connector side effect or production multi-tenancy.
- Real Chromium L1 and Chrome-CDP L2 smoke acceptance passed on 2026-09-03; the
  archived reports are `evals/reports/browser-l1-20260903T093547Z.json` and
  `evals/reports/browser-l2-20260903T093647Z.json`. They prove browser capture and
  screenshot/Evidence/Artifact/source-receipt persistence, not model quality or GUI L3.
- The autonomous standing-intent watcher, Feishu Run/reply delivery with durable
  channel identity binding, trusted-gateway principal/project authorization,
  governed memory lifetime/export/deletion, and the reviewed commercial
  `send_message` adapter are implemented and covered locally.
- No real Feishu tenant or authorized commercial test account has completed those
  external E2Es. The trusted gateway is a single-deployment-tenant principal boundary,
  not production multi-tenancy; `/api/auth/status` remains `multi_tenant: false`.
- The executable offline Snapshot gate currently contains 61 cases. It does not replace
  the still-required real commercial-connector Opportunity-to-Action E2E.

真端点验收（花钱，环境变量门控）：Green Path 3 连过与 Provider 合约测试见
[`backend/README.md`](backend/README.md)；评测体系与报告见 [`evals/README.md`](evals/README.md)。

The workspace keeps illustrative examples as an empty-state, then switches to the durable backend project ledger as soon as a live analysis starts. Commercial conclusions still require verified sources and user review.
