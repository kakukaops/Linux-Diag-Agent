# llm/ — LLM Provider Abstraction

## Three backends

| Backend | Use case | Auth |
|---------|----------|------|
| `claude_code` | Default; wraps `claude -p` subprocess, inherits Claude Code OAuth | None (uses logged-in session) |
| `openai_compat` | OpenRouter, DeepSeek, or any OpenAI-compatible API | `llm.endpoints.api_key` in `configs/local.yaml` |
| `vllm` | Self-hosted vLLM server | No key (or `OPENAI_API_KEY` env var) |

Switch backend per-role in `configs/local.yaml`:
```yaml
llm:
  chat:
    backend: openai_compat        # or claude_code, vllm
    model: deepseek/deepseek-v4-flash:free
  navigator:
    backend: openai_compat
    model: deepseek/deepseek-v4-flash:free
  endpoints:
    openai_compat: https://openrouter.ai/api/v1
    api_key: sk-or-v1-...         # gitignored via local.yaml
```

## Module layout

```
llm/
  provider/
    base.py              LLMProvider protocol, ChatRequest/Response schema
    registry.py          get_provider(role) → LLMProvider instance
    claude_code.py       subprocess adapter (claude -p)
    openai_compat.py     httpx OpenAI-compatible adapter
    anthropic_provider.py direct Anthropic SDK (needs ANTHROPIC_API_KEY)
    translation/
      openai_to_anthropic.py  schema translation layer
      stream_parser.py        stream-json event parser for claude -p output
  cache/
    pg_cache.py          PG-backed prompt cache (keyed by (model, messages hash))
  rate_limit/
    sliding_window.py    Token bucket / sliding window; 40 msg / 5h for claude_code
  observability/
    logger.py            JSONL log per LLM call (data/logs/)
```

## API key priority (openai_compat)

```python
api_key = (
    os.environ.get("OPENAI_API_KEY")
    or os.environ.get("OPENAI_COMPAT_API_KEY")
    or cfg.llm.endpoints.api_key   # from local.yaml
    or "none"
)
```

Never hardcode keys. Never commit `configs/local.yaml`.

## Rate limit guard (claude_code backend)

Pro subscription: ~45 messages / 5h window. Configured as 40 msg / 5h (5-message buffer).

- `budget.remaining_messages() < 3` → skip PageIndex traversal
- `budget.remaining_messages() < 1` → skip LLM rerank
- Query parse (navigator) always fires

`self_consistency_k` defaults to 1 in all configs. Set to 3 only for evaluation runs.

## Adding a new provider

1. Implement `LLMProvider` protocol from `llm/provider/base.py`
2. Register in `llm/provider/registry.py` `_BACKEND_MAP`
3. Add backend name to `LLMRoleConfig.backend` literal type in `configs/config.py`
4. Add endpoint config key to `LLMEndpointsConfig` if needed

## PG cache

`pg_cache.py` caches (model, SHA256(messages)) → response. TTL = 30 days. Bypass by adding `"eval"` or `"force_fresh"` to request metadata. Used to avoid re-charging Pro quota on identical evaluation queries.
