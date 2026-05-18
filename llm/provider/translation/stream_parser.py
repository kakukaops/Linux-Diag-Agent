"""Parse claude CLI stream-json output → ChatResponse / ChatChunk sequence.

The `claude -p --output-format stream-json --verbose --bare` command emits
newline-delimited JSON objects with a `type` field.  We accumulate deltas and
emit ChatChunk objects so the caller gets incremental text, then a final
ChatResponse once the stream closes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from llm.provider.base import ChatChunk, ChatResponse, ToolCall, Usage


def parse_stream_json(lines: Iterator[str]) -> Iterator[ChatChunk]:
    """Yield ChatChunk objects from claude stream-json lines.

    Callers should collect all chunks and call build_response() at the end.
    """
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _handle_event(event)


def _handle_event(event: dict[str, Any]) -> Iterator[ChatChunk]:
    etype = event.get("type", "")

    if etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            yield ChatChunk(delta=delta.get("text", ""))
        elif delta.get("type") == "input_json_delta":
            # Tool call argument streaming — accumulate downstream
            yield ChatChunk(delta="")

    elif etype == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        finish = _map_finish_reason(stop_reason)
        usage_data = event.get("usage", {})
        usage = Usage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
        )
        yield ChatChunk(finish_reason=finish, usage=usage)

    elif etype == "message_stop":
        # Signals the very end; already handled by message_delta
        pass


def build_response(chunks: list[ChatChunk], provider: str = "claude_code") -> ChatResponse:
    """Assemble a final ChatResponse from the accumulated chunk list."""
    text = "".join(c.delta for c in chunks)
    finish_reason = next(
        (c.finish_reason for c in reversed(chunks) if c.finish_reason),
        "stop",
    )
    usage = next((c.usage for c in reversed(chunks) if c.usage), Usage())
    return ChatResponse(
        content=text,
        finish_reason=finish_reason,  # type: ignore[arg-type]
        usage=usage,
        provider=provider,
    )


def _map_finish_reason(reason: str | None) -> str:
    mapping = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
    }
    return mapping.get(reason or "", "stop")


def parse_tool_calls_from_message(raw_message: dict[str, Any]) -> list[ToolCall]:
    """Extract ToolCall objects from a completed Anthropic message response."""
    import json as _json

    calls: list[ToolCall] = []
    for block in raw_message.get("content", []):
        if block.get("type") == "tool_use":
            calls.append(ToolCall(
                id=block.get("id", ""),
                function={
                    "name": block.get("name", ""),
                    "arguments": _json.dumps(block.get("input", {})),
                },
            ))
    return calls
