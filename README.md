# OPHIRA AI

**面向出海小 B 的 AI 全球商业系统**

> 发现世界所需，连接能够供应的人。  
> *Discover what the world needs. Connect who can supply it.*

## 产品定位

俄斐 AI 连接全球需求、商品、供应链和客户，帮助小团队从市场机会走向真实成交。

## 我们连接什么

- 全球需求
- 商品与供给
- 供应链
- 客户与成交机会

## 核心品牌主张

**发现世界所需，连接能够供应的人。**

**Discover what the world needs. Connect who can supply it.**

## 名字背后的精神

“OPHIRA / 俄斐”所承载的圣经精神，不是鼓励人追逐黄金，而是：

> 运用智慧认识资源、组织供给，并把有价值的东西带到真正需要它的地方。

这也是 OPHIRA AI 希望践行的商业理念：让需求被看见，让供给被组织，让价值跨越边界，抵达真正需要它的人。

## Frontend

The repository now contains two connected React surfaces:

- `/` — the OPHIRA marketing site and product story.
- `/app` — an interactive global opportunity workspace prototype.

### Run locally

Start the backend from the repository root:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e './backend[dev]'
cp backend/.env.example backend/.env
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload
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

真端点验收（花钱，环境变量门控）：Green Path 3 连过与 Provider 合约测试见
[`backend/README.md`](backend/README.md)；评测体系与报告见 [`evals/README.md`](evals/README.md)。

The workspace keeps illustrative examples as an empty-state, then switches to the durable backend project ledger as soon as a live analysis starts. Commercial conclusions still require verified sources and user review.
