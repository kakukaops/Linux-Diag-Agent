"""M1 anthropic provider — direct API key path (non-default, ADR-004)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from threading import Semaphore

import anthropic as _anthropic

from llm.provider.base import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    LLMProviderError,
    Usage,
)
from llm.provider.translation.openai_to_anthropic import to_anthropic_request
from llm.provider.translation.stream_parser import (
    build_response,
    parse_tool_calls_from_message,
)


class AnthropicProvider:
    """Calls Anthropic API directly with an API key (not claude_code OAuth)."""

    provider_name = "anthropic"

    def __init__(self, role: str = "chat") -> None:
        from configs.config import get_config
        cfg = get_config()
        role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
        self._model = role_cfg.model
        self._timeout = role_cfg.timeout_seconds
        self._semaphore = Semaphore(role_cfg.concurrency)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise LLMProviderError(
                "ANTHROPIC_API_KEY env var not set",
                code="auth",
                provider=self.provider_name,
            )
        self._client = _anthropic.Anthropic(api_key=api_key)

    def chat(self, req: ChatRequest) -> ChatResponse:
        chunks = list(self.chat_stream(req))
        return build_response(chunks, provider=self.provider_name)

    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]:
        body = to_anthropic_request(req, model=req.model or self._model)
        with self._semaphore:
            try:
                with self._client.messages.stream(**body) as stream:
                    for text in stream.text_stream:
                        yield ChatChunk(delta=text)
                    msg = stream.get_final_message()
                    yield ChatChunk(
                        finish_reason=_map(msg.stop_reason),
                        usage=Usage(
                            input_tokens=msg.usage.input_tokens,
                            output_tokens=msg.usage.output_tokens,
                        ),
                    )
            except _anthropic.RateLimitError as e:
                raise LLMProviderError(str(e), code="rate_limited",
                                       retry_after_seconds=300.0, provider=self.provider_name)
            except _anthropic.AuthenticationError as e:
                raise LLMProviderError(str(e), code="auth", provider=self.provider_name)
            except Exception as e:
                raise LLMProviderError(str(e), code="unknown", provider=self.provider_name)

    def health_check(self) -> bool:
        try:
            self._client.models.list()
            return True
        except Exception:
            return False


def _map(stop_reason: str | None) -> str:
    return {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
    }.get(stop_reason or "", "stop")
