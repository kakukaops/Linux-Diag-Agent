"""OpenAI Chat Completions ↔ Anthropic Messages schema translation.

Used by both claude_code (subprocess/agent_sdk) and anthropic providers.
"""

from __future__ import annotations

from typing import Any

from llm.provider.base import ChatRequest, Message, ToolSchema


def to_anthropic_messages(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """Split messages into (system_prompt, anthropic_messages).

    Anthropic API keeps the system prompt separate from the messages array.
    """
    system: str | None = None
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            # Concatenate multiple system messages (unusual but handle gracefully)
            system = (system + "\n\n" + _content_str(msg)) if system else _content_str(msg)
            continue

        if msg.role == "tool":
            # OpenAI "tool" role → Anthropic "user" role with tool_result block
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": _content_str(msg),
                }],
            })
            continue

        if msg.role == "assistant" and msg.tool_calls:
            # Assistant turn with tool calls → tool_use blocks
            content_blocks: list[dict[str, Any]] = []
            if msg.content:
                content_blocks.append({"type": "text", "text": _content_str(msg)})
            for tc in msg.tool_calls:
                import json
                args = tc.function.get("arguments", "{}")
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function["name"],
                    "input": json.loads(args) if isinstance(args, str) else args,
                })
            result.append({"role": "assistant", "content": content_blocks})
            continue

        result.append({"role": msg.role, "content": _content_str(msg)})

    return system, result


def to_anthropic_tools(tools: list[ToolSchema] | None) -> list[dict[str, Any]]:
    """Convert OpenAI tool schemas to Anthropic tool format."""
    if not tools:
        return []
    return [
        {
            "name": t.function.name,
            "description": t.function.description,
            "input_schema": t.function.parameters,
        }
        for t in tools
    ]


def to_anthropic_request(req: ChatRequest, model: str) -> dict[str, Any]:
    """Build a full Anthropic Messages API request body."""
    system, messages = to_anthropic_messages(req.messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens or 8192,
    }
    if system:
        body["system"] = system
    if req.tools:
        body["tools"] = to_anthropic_tools(req.tools)
        body["tool_choice"] = _map_tool_choice(req.tool_choice)
    if req.temperature != 0.0:
        body["temperature"] = req.temperature
    if req.response_format:
        # Anthropic doesn't support response_format natively;
        # inject JSON instruction into system prompt
        json_hint = "\n\nRespond with valid JSON only, no prose."
        body["system"] = (body.get("system") or "") + json_hint
    return body


def _content_str(msg: Message) -> str:
    if isinstance(msg.content, str):
        return msg.content
    if isinstance(msg.content, list):
        return "".join(p.text or "" for p in msg.content if p.type == "text")
    return ""


def _map_tool_choice(tc: Any) -> dict[str, Any]:
    if tc is None or tc == "auto":
        return {"type": "auto"}
    if tc == "required":
        return {"type": "any"}
    if tc == "none":
        return {"type": "none"}
    if isinstance(tc, dict) and "function" in tc:
        return {"type": "tool", "name": tc["function"]["name"]}
    return {"type": "auto"}
