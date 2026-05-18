"""M1 LLM Provider abstraction — OpenAI Chat Completions schema (ADR-003)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ── Wire types (OpenAI Chat Completions schema) ───────────────────────────

class ContentPart(BaseModel):
    type: Literal["text", "image_url"] = "text"
    text: str | None = None
    image_url: dict[str, Any] | None = None
    # Reserved for Anthropic Prompt Caching (MVP: not sent downstream)
    cache_control: dict[str, Any] | None = None


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")
    type: Literal["function"] = "function"
    function: dict[str, Any]            # {name: str, arguments: str (JSON-encoded)}


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None     # required when role="tool"
    name: str | None = None


class ToolFunction(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema


class ToolSchema(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str = ""                     # resolved by provider from config if empty
    tools: list[ToolSchema] | None = None
    tool_choice: Literal["auto", "required", "none"] | dict[str, Any] | None = None
    temperature: float = 0.0            # 0 → deterministic → PG cache eligible
    max_tokens: int | None = None
    stream: bool = True                 # streaming enabled by default (CLI needs it)
    response_format: dict[str, Any] | None = None   # {"type": "json_object"} etc.
    metadata: dict[str, Any] = Field(default_factory=dict)   # trace_id, sop_id, …


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0          # Anthropic Prompt Cache; 0 until enabled
    cache_write_tokens: int = 0
    cost_usd_est: float = 0.0


class ChatResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] = "stop"
    usage: Usage = Field(default_factory=Usage)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    provider: str = ""
    cached: bool = False


class ChatChunk(BaseModel):
    """Streaming delta chunk."""
    delta: str = ""
    tool_call_delta: ToolCall | None = None
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] | None = None
    usage: Usage | None = None          # only on final chunk


# ── Error model ───────────────────────────────────────────────────────────

class LLMProviderError(Exception):
    """Unified error raised by all LLM provider implementations."""

    def __init__(
        self,
        message: str,
        code: str = "unknown",
        retry_after_seconds: float | None = None,
        raw_error: dict[str, Any] | None = None,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        # Codes: rate_limited | context_exceeded | auth | tool_schema_invalid
        #        content_filter | transient | unknown
        self.retry_after_seconds = retry_after_seconds
        self.raw_error = raw_error or {}
        self.provider = provider

    def is_retryable(self) -> bool:
        return self.code in {"rate_limited", "transient"}


# ── Provider Protocol ─────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
    """All LLM backends must implement this protocol."""

    provider_name: str

    def chat(self, req: ChatRequest) -> ChatResponse:
        """Blocking synchronous chat completion."""
        ...

    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]:
        """Streaming chat — yields ChatChunk deltas."""
        ...

    def health_check(self) -> bool:
        """Return True if the provider/backend is reachable."""
        ...


# ── Factory helper ────────────────────────────────────────────────────────

def build_provider(role: str = "chat") -> LLMProvider:
    """
    Instantiate the configured LLM provider for the given role ('chat' | 'navigator').

    Import is deferred so unused providers don't require their deps at startup.
    """
    from configs.config import get_config

    cfg = get_config()
    role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
    backend = role_cfg.backend

    if backend == "claude_code":
        from llm.provider.claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(role=role)
    if backend in {"openai_compat", "vllm", "ollama"}:
        from llm.provider.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(role=role)
    if backend == "anthropic":
        from llm.provider.anthropic_provider import AnthropicProvider
        return AnthropicProvider(role=role)

    raise ValueError(f"Unknown LLM backend: {backend!r}")
