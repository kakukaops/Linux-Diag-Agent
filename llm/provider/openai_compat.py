"""M1 openai_compat provider — wraps any OpenAI-compatible endpoint.

Covers: OpenAI / DeepSeek / Moonshot / 智谱 / 百炼 / vLLM / ollama.
The vllm and ollama providers are thin subclasses that set default endpoints.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from threading import Semaphore

import openai

from llm.provider.base import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    LLMProviderError,
    ToolCall,
    Usage,
)


class OpenAICompatProvider:
    """Generic OpenAI Chat Completions provider (native schema, no translation)."""

    provider_name = "openai_compat"

    def __init__(self, role: str = "chat", endpoint: str | None = None) -> None:
        from configs.config import get_config
        cfg = get_config()
        role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
        self._model = role_cfg.model
        self._timeout = role_cfg.timeout_seconds
        self._semaphore = Semaphore(role_cfg.concurrency)

        base_url = endpoint or cfg.llm.endpoints.openai_compat or None
        api_key = _read_api_key("OPENAI_API_KEY", "OPENAI_COMPAT_API_KEY") or "none"
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)

    # ── Public interface ─────────────────────────────────────────────────

    def chat(self, req: ChatRequest) -> ChatResponse:
        chunks = list(self.chat_stream(req))
        return _assemble(chunks, provider=self.provider_name)

    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]:
        params = _build_params(req, self._model)
        with self._semaphore:
            try:
                stream = self._client.chat.completions.create(**params, stream=True,
                                                              timeout=self._timeout)
                for chunk in stream:
                    choice = chunk.choices[0] if chunk.choices else None
                    if not choice:
                        continue
                    delta = choice.delta
                    text = delta.content or ""
                    finish = choice.finish_reason
                    usage: Usage | None = None
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = Usage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                        )
                    yield ChatChunk(delta=text, finish_reason=finish, usage=usage)
            except openai.RateLimitError as e:
                raise LLMProviderError(str(e), code="rate_limited",
                                       retry_after_seconds=60.0, provider=self.provider_name)
            except openai.AuthenticationError as e:
                raise LLMProviderError(str(e), code="auth", provider=self.provider_name)
            except openai.BadRequestError as e:
                raise LLMProviderError(str(e), code="tool_schema_invalid",
                                       provider=self.provider_name)
            except Exception as e:
                raise LLMProviderError(str(e), code="unknown", provider=self.provider_name)

    def health_check(self) -> bool:
        try:
            self._client.models.list()
            return True
        except Exception:
            return False


class VllmProvider(OpenAICompatProvider):
    """vLLM local server (v1.3 full activation)."""

    provider_name = "vllm"

    def __init__(self, role: str = "chat") -> None:
        from configs.config import get_config
        endpoint = get_config().llm.endpoints.vllm
        super().__init__(role=role, endpoint=endpoint)


class OllamaProvider(OpenAICompatProvider):
    """Ollama local server (CI / dev smoke tests)."""

    provider_name = "ollama"

    def __init__(self, role: str = "chat") -> None:
        from configs.config import get_config
        endpoint = get_config().llm.endpoints.ollama
        super().__init__(role=role, endpoint=endpoint)


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_params(req: ChatRequest, default_model: str) -> dict:
    messages = [
        {"role": m.role, "content": _content(m)}
        for m in req.messages
    ]
    params: dict = {
        "model": req.model or default_model,
        "messages": messages,
        "temperature": req.temperature,
    }
    if req.max_tokens:
        params["max_tokens"] = req.max_tokens
    if req.tools:
        params["tools"] = [t.model_dump() for t in req.tools]
    if req.tool_choice is not None:
        params["tool_choice"] = req.tool_choice
    if req.response_format:
        params["response_format"] = req.response_format
    return params


def _content(msg) -> str | list:
    if isinstance(msg.content, list):
        return [p.model_dump(exclude_none=True) for p in msg.content]
    return msg.content or ""


def _assemble(chunks: list[ChatChunk], provider: str) -> ChatResponse:
    text = "".join(c.delta for c in chunks)
    finish = next((c.finish_reason for c in reversed(chunks) if c.finish_reason), "stop")
    usage = next((c.usage for c in reversed(chunks) if c.usage), Usage())
    return ChatResponse(content=text, finish_reason=finish,  # type: ignore[arg-type]
                        usage=usage, provider=provider)


def _read_api_key(*env_names: str) -> str:
    import os
    for name in env_names:
        val = os.environ.get(name, "")
        if val:
            return val
    return ""
