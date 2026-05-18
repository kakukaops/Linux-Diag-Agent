"""Unit tests for llm/provider/base.py types."""

import pytest
from llm.provider.base import (
    ChatRequest,
    ChatResponse,
    LLMProviderError,
    Message,
    ToolCall,
    ToolFunction,
    ToolSchema,
    Usage,
)


def test_chat_request_defaults():
    req = ChatRequest(messages=[Message(role="user", content="hello")])
    assert req.temperature == 0.0
    assert req.stream is True
    assert req.tools is None


def test_message_roundtrip():
    m = Message(role="user", content="test")
    assert m.role == "user"
    assert m.content == "test"


def test_tool_schema_build():
    schema = ToolSchema(
        function=ToolFunction(
            name="search_code",
            description="Search kernel source",
            parameters={"type": "object", "properties": {}},
        )
    )
    assert schema.type == "function"
    assert schema.function.name == "search_code"


def test_llm_provider_error_retryable():
    e = LLMProviderError("rate limit hit", code="rate_limited", provider="claude_code")
    assert e.is_retryable() is True

    e2 = LLMProviderError("auth failed", code="auth", provider="claude_code")
    assert e2.is_retryable() is False


def test_usage_defaults():
    u = Usage()
    assert u.input_tokens == 0
    assert u.cache_read_tokens == 0


def test_chat_response_build():
    resp = ChatResponse(content="hello world", provider="claude_code")
    assert resp.cached is False
    assert resp.finish_reason == "stop"
