"""Unit tests for openai_compat tool-calling wire support (M22 spike).

The fiddly part is streaming reconstruction: OpenAI streams a tool call's
`arguments` JSON as fragments across many chunks. These tests exercise the
pure accumulation helpers and the param/assemble functions — no network.
"""

from types import SimpleNamespace as NS

from llm.provider.base import (
    ChatChunk, ChatRequest, Message, ToolCall, ToolFunction, ToolSchema,
)
from llm.provider.openai_compat import (
    _accumulate_tool_call, _assemble, _build_params, _drain_tool_calls,
)


def _tcd(index, id=None, name=None, arguments=None):
    """Fake streaming tool_call delta — mimics the OpenAI SDK object shape."""
    fn = (NS(name=name, arguments=arguments)
          if (name is not None or arguments is not None) else None)
    return NS(index=index, id=id, function=fn)


# ── streaming tool-call reconstruction ──────────────────────────────────────

def test_accumulate_single_tool_call_fragmented():
    buf: dict = {}
    # OpenAI sends id+name first, then arguments JSON in fragments
    _accumulate_tool_call(buf, _tcd(0, id="call_x", name="search_commits"))
    _accumulate_tool_call(buf, _tcd(0, arguments='{"keyw'))
    _accumulate_tool_call(buf, _tcd(0, arguments='ords": "oom"}'))
    calls = _drain_tool_calls(buf)
    assert len(calls) == 1
    assert calls[0].id == "call_x"
    assert calls[0].function == {"name": "search_commits",
                                 "arguments": '{"keywords": "oom"}'}


def test_accumulate_parallel_tool_calls():
    buf: dict = {}
    _accumulate_tool_call(buf, _tcd(0, id="c0", name="search_cve", arguments="{}"))
    _accumulate_tool_call(buf, _tcd(1, id="c1", name="search_bugs", arguments="{}"))
    calls = _drain_tool_calls(buf)
    assert [c.function["name"] for c in calls] == ["search_cve", "search_bugs"]


def test_drain_empty_buffer():
    assert _drain_tool_calls({}) == []


def test_drain_skips_nameless_fragment():
    buf: dict = {}
    _accumulate_tool_call(buf, _tcd(0, arguments="{}"))   # never received a name
    assert _drain_tool_calls(buf) == []


# ── outgoing message serialization ──────────────────────────────────────────

def test_build_params_round_trips_tool_history():
    req = ChatRequest(messages=[
        Message(role="user", content="diagnose"),
        Message(role="assistant", content="",
                tool_calls=[ToolCall(id="c1", function={"name": "f", "arguments": "{}"})]),
        Message(role="tool", tool_call_id="c1", name="f", content="result"),
    ])
    msgs = _build_params(req, "default-model")["messages"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "c1"


def test_build_params_sends_tool_schemas():
    req = ChatRequest(
        messages=[Message(role="user", content="x")],
        tools=[ToolSchema(function=ToolFunction(
            name="f", description="d", parameters={"type": "object"}))],
        tool_choice="auto",
    )
    params = _build_params(req, "m")
    assert params["tools"][0]["function"]["name"] == "f"
    assert params["tool_choice"] == "auto"


# ── response assembly ───────────────────────────────────────────────────────

def test_assemble_reconstructs_tool_calls_and_normalizes_finish():
    chunks = [
        ChatChunk(delta="", finish_reason="stop"),
        ChatChunk(tool_call_delta=ToolCall(id="c1",
                                           function={"name": "f", "arguments": "{}"})),
    ]
    resp = _assemble(chunks, provider="openai_compat")
    assert len(resp.tool_calls) == 1
    assert resp.finish_reason == "tool_calls"   # normalized up from "stop"


def test_assemble_plain_text_has_no_tool_calls():
    chunks = [ChatChunk(delta="hello "),
              ChatChunk(delta="world", finish_reason="stop")]
    resp = _assemble(chunks, provider="openai_compat")
    assert resp.content == "hello world"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
