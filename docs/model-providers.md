# Anthropic-compatible model providers

OPHIRA uses Claude Agent SDK, which launches Claude Code. A provider therefore needs
an Anthropic Messages-compatible endpoint. Do not use its OpenAI Chat Completions URL.

Copy `backend/.env.example` to `backend/.env`, then configure one provider. Use the API
key issued for the same product, plan, and region as the selected Base URL.

| Provider | `ANTHROPIC_BASE_URL` | Credential variable | Current example model |
| --- | --- | --- | --- |
| Anthropic | leave unset | `ANTHROPIC_API_KEY` | leave unset or a Claude model ID |
| DeepSeek | `https://api.deepseek.com/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `deepseek-v4-pro[1m]` |
| Kimi Code | `https://api.kimi.com/coding/` | `ANTHROPIC_API_KEY` | `k3-256k` |
| GLM | `https://open.bigmodel.cn/api/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `glm-5.2[1m]` |
| Qwen, pay-as-you-go (Beijing) | `https://dashscope.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `qwen3.7-max` |
| Qwen Coding Plan | `https://coding.dashscope.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `qwen3.7-plus` |
| Qwen Token Plan | `https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic` | `ANTHROPIC_AUTH_TOKEN` | use a model supported by the plan |

Alibaba documents Coding Plan and Token Plan endpoints for supported interactive AI
tools. For a deployed backend service, use the pay-as-you-go Anthropic endpoint unless
your plan terms explicitly permit the workload.

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
