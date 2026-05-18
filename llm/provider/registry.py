"""LLM provider registry — resolve provider name to LLMProvider instance."""

from __future__ import annotations

import functools

from llm.provider.base import LLMProvider


@functools.lru_cache(maxsize=8)
def get_provider(provider_name: str) -> LLMProvider:
    """Return a cached LLMProvider instance for the given provider name."""
    name = provider_name.lower()
    if name == "claude_code":
        from llm.provider.claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider()
    if name == "anthropic":
        from llm.provider.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if name in ("openai", "openai_compat"):
        from llm.provider.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider()
    if name == "vllm":
        from llm.provider.openai_compat import VllmProvider
        return VllmProvider()
    if name == "ollama":
        from llm.provider.openai_compat import OllamaProvider
        return OllamaProvider()
    raise ValueError(f"Unknown LLM provider: {provider_name!r}")
