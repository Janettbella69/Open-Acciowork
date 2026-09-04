# TradeMind

**面向跨境团队的 AI 全球商业系统**

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
pairing preparation (`/app/mobile`).

Tenant Skills keep immutable, content-addressed package history with per-run snapshots,
version inspection, reviewed rollback, bounded storage, and active-session-safe runtime
leases. Workspace results can also render typed evidence, decision, and artifact cards
whose factual fields are joined from the server ledger rather than authored by the model.

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
  the still-required real Provider + real connector Opportunity-to-Action E2E.

真端点验收（花钱，环境变量门控）：Green Path 3 连过与 Provider 合约测试见
[`backend/README.md`](backend/README.md)；评测体系与报告见 [`evals/README.md`](evals/README.md)。

The workspace keeps illustrative examples as an empty-state, then switches to the durable backend project ledger as soon as a live analysis starts. Commercial conclusions still require verified sources and user review.
