"""M1 openai_compat provider — wraps any OpenAI-compatible endpoint.

Covers: OpenAI / DeepSeek / Moonshot / 智谱 / 百炼 / vLLM / ollama.
The vllm and ollama providers are thin subclasses that set default endpoints.

Rate limiting strategy:
  - `rate_limit.rate_limit_rpm > 0` → proactive per-minute sliding-window limiter
    (acquires a slot before each call, blocks until one is free)
  - On 429 response → exponential backoff retry up to `retry.max_attempts` times
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Generic OpenAI Chat Completions provider (native schema, no translation)."""

    provider_name = "openai_compat"

    def __init__(self, role: str = "chat", endpoint: str | None = None) -> None:
        from configs.config import get_config
        from llm.rate_limit.sliding_window import get_endpoint_limiter
        cfg = get_config()
        role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
        self._model = role_cfg.model
        self._timeout = role_cfg.timeout_seconds
        self._semaphore = Semaphore(role_cfg.concurrency)
        self._retry = role_cfg.retry

        base_url = endpoint or cfg.llm.endpoints.openai_compat or None
        api_key = (_read_api_key("OPENAI_API_KEY", "OPENAI_COMPAT_API_KEY")
                   or cfg.llm.endpoints.api_key
                   or "none")
        # max_retries=0: disable SDK auto-retry so our application-level retry
        # (with rate-limiter re-acquire) is the only retry path.
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

        # Shared account-level per-minute limiter (keyed by endpoint URL so all
        # roles using the same API share one counter, preventing total > rpm).
        rpm = role_cfg.rate_limit.rate_limit_rpm
        endpoint_key = base_url or "openai_compat_default"
        self._rpm_limiter = get_endpoint_limiter(endpoint_key, rpm) if rpm > 0 else None

    # ── Public interface ─────────────────────────────────────────────────

    def chat(self, req: ChatRequest) -> ChatResponse:
        """Call with automatic retry on rate-limit AND transient network errors.

        Retries on:
          - LLMProviderError(code='rate_limited')  — upstream 429
          - openai.APIConnectionError / APITimeoutError  — TCP/DNS/timeout
          - openai.APIError(5xx)                   — upstream transient
        Under concurrency=3 these were observed as ~50% case-failure rate
        because the SDK retry path is disabled (max_retries=0); without
        app-level retry they're fatal.
        """
        last_exc: Exception | None = None
        max_attempts = self._retry.max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                chunks = list(self.chat_stream(req))
                return _assemble(chunks, provider=self.provider_name)
            except LLMProviderError as exc:
                if exc.code != "rate_limited" or attempt >= max_attempts:
                    raise
                backoff = min(
                    (exc.retry_after_seconds or 0) or self._retry.backoff_initial * (2 ** (attempt - 1)),
                    self._retry.backoff_max,
                )
                logger.warning("429 rate limited, waiting %.1fs then retry %d/%d",
                               backoff, attempt + 1, max_attempts)
                time.sleep(backoff)
                last_exc = exc
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                if attempt >= max_attempts:
                    raise
                backoff = min(self._retry.backoff_initial * (2 ** (attempt - 1)),
                              self._retry.backoff_max)
                logger.warning("network error (%s), retrying in %.1fs (%d/%d)",
                               type(exc).__name__, backoff, attempt + 1, max_attempts)
                time.sleep(backoff)
                last_exc = exc
            except openai.APIStatusError as exc:
                # Retry on 5xx (upstream transient). 4xx (other than 429) raise immediately.
                if exc.status_code < 500 or attempt >= max_attempts:
                    raise
                backoff = min(self._retry.backoff_initial * (2 ** (attempt - 1)),
                              self._retry.backoff_max)
                logger.warning("upstream %d, retrying in %.1fs (%d/%d)",
                               exc.status_code, backoff, attempt + 1, max_attempts)
                time.sleep(backoff)
                last_exc = exc
        raise last_exc  # type: ignore[misc]

    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]:
        # Proactive rate limit: block until a per-minute slot is free
        if self._rpm_limiter:
            self._rpm_limiter.acquire()

        params = _build_params(req, self._model)
        with self._semaphore:
            try:
                stream = self._client.chat.completions.create(
                    **params, stream=True,
                    stream_options={"include_usage": True},
                    timeout=self._timeout,
                )
                tool_buf: dict[int, dict] = {}
                for chunk in stream:
                    choice = chunk.choices[0] if chunk.choices else None
                    if not choice:
                        continue
                    delta = choice.delta
                    text = delta.content or ""
                    finish = choice.finish_reason
                    for tcd in (getattr(delta, "tool_calls", None) or []):
                        _accumulate_tool_call(tool_buf, tcd)
                    usage: Usage | None = None
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = Usage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                        )
                    yield ChatChunk(delta=text, finish_reason=finish, usage=usage)
                # OpenAI streams tool-call args as fragments — emit once whole
                for tc in _drain_tool_calls(tool_buf):
                    yield ChatChunk(tool_call_delta=tc)
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
    messages = [_msg_dict(m) for m in req.messages]
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


def _msg_dict(msg) -> dict:
    """Serialize one Message to the OpenAI wire dict, keeping tool-call fields.

    Without tool_calls / tool_call_id, a multi-turn ReAct conversation cannot
    be replayed back to the model (the assistant's requested calls and the
    tool results would be lost). M22 needs the full history round-tripped.
    """
    d: dict = {"role": msg.role, "content": _content(msg)}
    if msg.tool_calls:
        d["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.name:
        d["name"] = msg.name
    return d


def _accumulate_tool_call(buf: dict[int, dict], tcd) -> None:
    """Merge one streaming tool-call delta into the index-keyed buffer.

    OpenAI streams a tool call as: index + id + name in the first delta, then
    the `arguments` JSON string in fragments across the following deltas.
    """
    idx = getattr(tcd, "index", 0) or 0
    slot = buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if getattr(tcd, "id", None):
        slot["id"] = tcd.id
    fn = getattr(tcd, "function", None)
    if fn is not None:
        if getattr(fn, "name", None):
            slot["name"] = fn.name
        if getattr(fn, "arguments", None):
            slot["arguments"] += fn.arguments


def _drain_tool_calls(buf: dict[int, dict]) -> list[ToolCall]:
    """Turn the accumulated buffer into ToolCall objects, in index order."""
    out: list[ToolCall] = []
    for idx in sorted(buf):
        slot = buf[idx]
        if not slot["name"]:
            continue
        kwargs: dict = {"function": {"name": slot["name"],
                                     "arguments": slot["arguments"] or "{}"}}
        if slot["id"]:
            kwargs["id"] = slot["id"]
        out.append(ToolCall(**kwargs))
    return out


def _assemble(chunks: list[ChatChunk], provider: str) -> ChatResponse:
    text = "".join(c.delta for c in chunks)
    finish = next((c.finish_reason for c in reversed(chunks) if c.finish_reason), "stop")
    usage = next((c.usage for c in reversed(chunks) if c.usage), Usage())
    tool_calls = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    if tool_calls and finish == "stop":
        finish = "tool_calls"          # some compat servers omit the signal
    return ChatResponse(content=text, tool_calls=tool_calls,
                        finish_reason=finish,  # type: ignore[arg-type]
                        usage=usage, provider=provider)


def _read_api_key(*env_names: str) -> str:
    import os
    for name in env_names:
        val = os.environ.get(name, "")
        if val:
            return val
    return ""
