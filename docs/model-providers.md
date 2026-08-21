# Anthropic-compatible model providers

OPHIRA uses Claude Agent SDK, which launches Claude Code. A provider therefore needs
an Anthropic Messages-compatible endpoint. Do not use its OpenAI Chat Completions URL.

Copy `backend/.env.example` to `backend/.env`, then configure one provider. Use the API
key issued for the same product, plan, and region as the selected Base URL.

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
back to the profile (PRD §4.9). Measured so far: DeepSeek native WebSearch ✅; image input
✗ on both DeepSeek and GLM (2026-08-21, `evals/reports/provider-contract-*`).

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
