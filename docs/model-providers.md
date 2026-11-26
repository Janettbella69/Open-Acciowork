# Anthropic-compatible model providers

TradeMind runs the Claude Agent SDK, which launches Claude Code, so the model endpoint must
speak the Anthropic Messages API. Use the provider's Anthropic-compatible URL, never its
OpenAI Chat Completions URL.

Configure one provider in the backend's environment (`backend/.env` locally,
`/etc/trademind/backend.local.env` on the server). The key must be issued for the same
product, plan and region as the URL.

    OPHIRA_MODEL_BASE_URL=https://api.deepseek.com/anthropic
    OPHIRA_MODEL=deepseek-v4-pro[1m]   # [1m]: tells Claude Code the window is 1M tokens
    OPHIRA_MODEL_AUTH_TOKEN=<key>
    # OPHIRA_MODEL_AUTH_ENV=ANTHROPIC_API_KEY   only for providers that want x-api-key

The key stays in the backend's model gateway (`backend/src/ophira_backend/gateway.py`);
the Agent's process only gets a per-process gateway token. `OPHIRA_MODEL` is mapped to every
model tier Claude Code may ask for (Haiku, Sonnet, Opus, Fable, subagents), so it never
substitutes an Anthropic model name on a third-party endpoint.

| Provider | `OPHIRA_MODEL_BASE_URL` | Example `OPHIRA_MODEL` |
| --- | --- | --- |
| Anthropic | leave unset (set `OPHIRA_MODEL_AUTH_ENV=ANTHROPIC_API_KEY`) | leave unset or a Claude model ID |
| DeepSeek | `https://api.deepseek.com/anthropic` | `deepseek-v4-pro[1m]` |
| Kimi open platform | `https://api.moonshot.cn/anthropic` (mainland) · `https://api.moonshot.ai/anthropic` (overseas) | `kimi-k3[1m]` |
| GLM | `https://open.bigmodel.cn/api/anthropic` | `glm-5.2[1m]` |
| Qwen, pay-as-you-go (Beijing) | `https://dashscope.aliyuncs.com/apps/anthropic` | `qwen3.7-max` |
| ~~Qwen Coding Plan~~ | ~~`https://coding.dashscope.aliyuncs.com/apps/anthropic`~~ | ⛔ not for this backend |
| ~~Kimi Code membership~~ | ~~`https://api.kimi.com/coding/`~~ | ⛔ not for this backend |

**Coding-plan keys are barred from this backend.** Alibaba's Coding Plan terms forbid
"automated scripts, application backends, or other non-interactive scenarios", Zhipu's
require self-built apps, sites, bots and SaaS to use the standard API, and Kimi Code is for
interactive coding tools only. The struck-out rows are listed so the wrong endpoint is
recognised, not used.

## What has been measured

Capabilities are measured against the real endpoint, never copied from vendor pages. These
measurements were made with Claude Agent SDK 0.2.143 before the 2026-11 rebuild; the reports
are kept at the tag `archive/pre-rebuild-2026-11-26` under `evals/reports/`.

- **DeepSeek `deepseek-v4-pro`** (2026-09-04, nine-case contract run: 7 passed, 2 degraded,
  none failed): text streaming, served-model identity, file tools, Bash, MCP, Plugin Skill
  discovery and **native WebSearch work**. **Image input does not**: a raw `image` block sent
  to `api.deepseek.com/anthropic` is answered with HTTP 200 while the image is silently
  dropped (`input_tokens: 101`; the model's own thinking says it cannot see the picture).
  A status code alone would have read as success. An invalid key makes the SDK wait instead of
  failing fast.
- **Kimi K3**: image input works (verified twice on `api.moonshot.cn/anthropic`, raw and
  through the Read tool; 224 input tokens for the payload DeepSeek charged 101 for — the gap
  separates a processed image from a discarded one). Native WebSearch works; **WebFetch is not
  available** on this endpoint.
- **`deepseek-v4-flash-vision-exp`** (2026-08-21): image input works on the same DeepSeek
  endpoint, native WebSearch works; the model is explicitly experimental (no published price
  or deprecation guarantee) and its context window is unmeasured.
- **GLM** image input: unknown (the account was out of balance when it was tested).

Not yet measured after the rebuild: a real turn through the new gateway on any of these
endpoints. Before relying on one, run a single short turn and read its log.

Official references:

- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [DeepSeek in Claude Code](https://api-docs.deepseek.com/zh-cn/guides/agent_integrations/claude_code)
- [Kimi Code in Claude Code](https://www.kimi.com/code/docs/third-party-tools/claude-code.html)
- [GLM Claude API compatibility](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)
- [Qwen / Model Studio in Claude Code](https://help.aliyun.com/en/model-studio/claude-code)
