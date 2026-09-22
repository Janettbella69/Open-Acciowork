# Anthropic-compatible model providers

OPHIRA uses Claude Agent SDK, which launches Claude Code. A provider therefore needs
an Anthropic Messages-compatible endpoint. Do not use its OpenAI Chat Completions URL.

Copy `backend/.env.example` to `backend/.env`, then configure one provider. Use the API
key issued for the same product, plan, and region as the selected Base URL.

The workspace model selector and Provider settings show the configured endpoint and
administrator-reported region, with data-sharing and cross-border transfer information.
Unknown regions and automatic routing stay explicit. This label does not verify or enforce
data residency; see [Provider data disclosure](provider-data-disclosure.md).

| Provider | `ANTHROPIC_BASE_URL` | Credential variable | Current example model |
| --- | --- | --- | --- |
| Anthropic | leave unset | `ANTHROPIC_API_KEY` | leave unset or a Claude model ID |
| DeepSeek | `https://api.deepseek.com/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `deepseek-v4-pro[1m]` |
| Kimi open platform | `https://api.moonshot.cn/anthropic` (mainland) · `https://api.moonshot.ai/anthropic` (overseas) | `ANTHROPIC_AUTH_TOKEN` | `kimi-k3[1m]` |
| ~~Kimi Code membership~~ | ~~`https://api.kimi.com/coding/`~~ | — | ⛔ **Not for this backend** — interactive coding tools only (PRD §4.9). Use the open platform row above. |
| GLM | `https://open.bigmodel.cn/api/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `glm-5.2[1m]` |
| Qwen, pay-as-you-go (Beijing) | `https://dashscope.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `qwen3.7-max` |
| Qwen Coding Plan | `https://coding.dashscope.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `qwen3.7-plus` |
| Qwen Token Plan | `https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | use a model supported by the plan |

**Coding-plan keys are barred from this backend** (red line, PRD §4.9): Alibaba's Coding
Plan terms forbid "automated scripts, application backends, or other non-interactive
scenarios", Zhipu's require self-built apps/sites/bots/SaaS to use the standard API, and
Kimi Code is positioned for interactive coding tools only. The rows above marked ⛔ are
listed so the wrong endpoint is recognised, not used — always pick the pay-as-you-go one.

Capability columns are never copied from vendor docs. Anything a run depends on — native
web tools, image input, context window — is measured by the contract suite and written
back to the profile (PRD §4.9). The current nine-case DeepSeek profile was rerun against the real
endpoint on 2026-09-04 with Claude Agent SDK 0.2.143: 7 cases passed and 2 were degraded,
with no failures (`evals/reports/provider-contract-deepseek-20260904.*`). Text streaming,
served-model identity, file tools, Bash, MCP, Plugin Skill discovery, and native WebSearch
passed. Image comprehension remains unavailable, and the SDK's invalid-token path still
requires OPHIRA's bounded preflight timeout instead of failing fast. The latter two results
match the 2026-08-21 measurements. DeepSeek native WebSearch ✅; DeepSeek image input ✗ —
confirmed through repeated agent Read probes and by posting a raw `image` block to
`api.deepseek.com/anthropic`, which
answers **HTTP 200 while silently dropping the image** (`input_tokens: 101`, and the
model's own thinking reads "图片是unsupported，我们看不到"). Note that shape: a status
code alone would have read as success. **Kimi K3 image input ✅** — verified twice on
`api.moonshot.cn/anthropic`, once raw and once through the agent's Read tool, reading
the planted digits correctly both times and spending 224 input tokens on the same
payload DeepSeek charged 101 for. That token gap is the tell: it separates a processed
image from a discarded one when both endpoints answer 200. GLM image input is
**unknown** — its account was out of balance, and the earlier "unsupported" line has
been retracted as an invalid measurement. **`deepseek-v4-flash-vision-exp` image input
✅** (released 2026-08-21, measured same day via the dedicated `deepseek-vision-exp`
profile): served on the same `api.deepseek.com/anthropic` endpoint, and the agent's Read
path recovered the planted digits — full contract run is 8 PASS + 1 DEGRADED, exactly
Kimi K3's shape, including native WebSearch, with the one degraded case being the same
invalid-token hang (bounded by the FR-06 preflight timeout). Caveats recorded on the
profile: the model is explicitly **experimental** (no pricing or deprecation guarantee
published), its text tier is V4-Flash rather than V4-Pro, and its context window is
unmeasured (`context_window_tokens: null` — the family's 1M claim is not inherited).
See `evals/reports/provider-contract-deepseek-vision-exp-20260821.*`.

Kimi remains the incumbent vision profile for FR-20's GUI ladder; DeepSeek
stays the main agent on the strength of its proven Green Path, contract, and L2
baselines. `deepseek-vision-exp` is a measured vision-capable fallback for that
seat — and stays the fallback, not the incumbent (decided 2026-08-21). Contract parity
alone doesn't justify the swap: an experimental model can be revised behind its model
string without an API error, and screenshot-judgment drift is exactly the failure FR-20
cannot observe yet (no vision-quality baseline). The seat is revisited on any of three
triggers: **graduation** (the `-Exp` suffix drops and the model reaches the official
price page → switch, collect the single-vendor win), **cost** (Kimi spend becomes
material at FR-20 volume → run a head-to-head screenshot-judging eval first, decide on
data), or **failure** (Kimi platform trouble or a contract regression → the fallback
takes the seat immediately). Kimi's full contract run is 8 PASS + 1 DEGRADED — **native WebSearch passes
there too**, so the "Kimi does not support it" note applies to WebFetch alone; the one
degraded case is the invalid-token hang both providers share, bounded by the FR-06
preflight timeout.

The main model can be configured with either `OPHIRA_CLAUDE_MODEL` or the official
`ANTHROPIC_MODEL` name. OPHIRA exports it as `ANTHROPIC_MODEL` and, unless explicitly
overridden, maps the Haiku, Fable, Sonnet, Opus, and subagent model variables to the same
model. This prevents Claude Code from substituting an Anthropic model ID while using a
third-party endpoint. Advanced mappings, effort, and context limits remain optional in
`backend/.env.example`.

Official references:

- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [DeepSeek in Claude Code](https://api-docs.deepseek.com/zh-cn/guides/agent_integrations/claude_code)
- [Kimi Code in Claude Code](https://www.kimi.com/code/docs/third-party-tools/claude-code.html)
- [GLM Claude API compatibility](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)
- [Qwen / Model Studio in Claude Code](https://help.aliyun.com/en/model-studio/claude-code)

`GET /api/runtime/provider` reports whether API credentials, a Base URL, and a model are
configured. It never returns the credential value.
