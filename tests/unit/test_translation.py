"""Unit tests for OpenAI ↔ Anthropic schema translation."""

import json
import pytest
from llm.provider.base import Message, ToolCall, ToolFunction, ToolSchema
from llm.provider.translation.openai_to_anthropic import (
    to_anthropic_messages,
    to_anthropic_tools,
    to_anthropic_request,
)
from llm.provider.base import ChatRequest


def test_system_message_extracted():
    messages = [
        Message(role="system", content="You are a Linux kernel expert."),
        Message(role="user", content="What is OOM?"),
    ]
    system, result = to_anthropic_messages(messages)
    assert system == "You are a Linux kernel expert."
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_tool_role_converted():
    messages = [
        Message(role="tool", content='{"result": "ok"}', tool_call_id="call_abc"),
    ]
    _, result = to_anthropic_messages(messages)
    assert result[0]["role"] == "user"
    assert result[0]["content"][0]["type"] == "tool_result"
    assert result[0]["content"][0]["tool_use_id"] == "call_abc"


def test_assistant_with_tool_calls():
    tc = ToolCall(
        id="call_xyz",
        function={"name": "search_code", "arguments": '{"query": "oom"}'},
    )
    messages = [Message(role="assistant", content="Searching...", tool_calls=[tc])]
    _, result = to_anthropic_messages(messages)
    blocks = result[0]["content"]
    assert any(b["type"] == "text" for b in blocks)
    assert any(b["type"] == "tool_use" for b in blocks)


def test_tools_conversion():
    tools = [
        ToolSchema(
            function=ToolFunction(
                name="lookup_symbol",
                description="Look up a kernel symbol",
                parameters={"type": "object", "properties": {}},
            )
        )
    ]
    result = to_anthropic_tools(tools)
    assert result[0]["name"] == "lookup_symbol"
    assert "input_schema" in result[0]


def test_full_request_build():
    req = ChatRequest(
        messages=[Message(role="user", content="diagnose OOM")],
        model="claude-sonnet-4-6",
    )
    body = to_anthropic_request(req, model="claude-sonnet-4-6")
    assert body["model"] == "claude-sonnet-4-6"
    assert len(body["messages"]) == 1
    assert "system" not in body
