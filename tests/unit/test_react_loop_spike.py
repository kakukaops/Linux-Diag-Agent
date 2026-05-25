"""Spike tests for the M22 ReAct loop prototype (agent/react/).

Drives the loop with a scripted fake provider — proves the loop threads
tool results back into the conversation and terminates correctly, with no
network. See docs/v2/M22_spike_findings.md.
"""

from llm.provider.base import ChatResponse, ToolCall, Usage
from agent.react.loop import run_react_loop
from agent.react.tool_registry import Tool, ToolRegistry


def _tool_call(name, args="{}"):
    return ToolCall(function={"name": name, "arguments": args})


def _resp_tools(*calls):
    return ChatResponse(content="", tool_calls=list(calls), finish_reason="tool_calls")


def _resp_final(text):
    return ChatResponse(content=text, finish_reason="stop")


class FakeProvider:
    """Replays scripted ChatResponses; records the requests it received."""

    provider_name = "fake"

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def chat(self, req):
        self.requests.append(req)
        return self._responses.pop(0)


def _registry():
    reg = ToolRegistry()
    reg.register(Tool("get_meminfo", "read /proc/meminfo",
                       {"type": "object", "properties": {}},
                       fn=lambda **kw: "MemFree: 100 kB", routes=frozenset({"kernel"})))
    reg.register(Tool("get_mce_log", "read MCE log",
                       {"type": "object", "properties": {}},
                       fn=lambda **kw: "no MCE", routes=frozenset({"hardware"})))
    return reg


# ── loop behaviour ──────────────────────────────────────────────────────────

def test_loop_dispatches_tool_then_diagnoses():
    provider = FakeProvider(
        _resp_tools(_tool_call("get_meminfo")),
        _resp_final("<final_answer>OOM from memory pressure</final_answer>"),
    )
    result = run_react_loop(
        provider=provider, registry=_registry(), route="kernel",
        system_prompt="sys", user_prompt="why OOM?",
    )
    assert result.verdict == "diagnosed"
    assert result.iterations == 2
    assert "OOM" in result.final_answer
    assert result.tool_trace == [
        {"step": 1, "tool": "get_meminfo", "args": "{}", "error": False}]
    # the 2nd LLM call must have seen the tool result threaded in
    second_call_msgs = provider.requests[1].messages
    assert any(m.role == "tool" and m.content == "MemFree: 100 kB"
               for m in second_call_msgs)


def test_loop_respects_max_iter():
    # distinct args each step → the repeat-call detector does not pre-empt the
    # iteration cap, so termination is genuinely MAX_ITER.
    provider = FakeProvider(*[
        _resp_tools(_tool_call("get_meminfo", args=f'{{"i": {i}}}'))
        for i in range(10)
    ])
    result = run_react_loop(
        provider=provider, registry=_registry(), route="kernel",
        system_prompt="s", user_prompt="u", max_iter=3,
    )
    assert result.verdict == "max_iter_reached"
    assert result.iterations == 3


def test_loop_feeds_tool_error_back():
    def _boom():
        raise RuntimeError("disk gone")

    reg = ToolRegistry()
    reg.register(Tool("get_disk", "d", {"type": "object", "properties": {}}, fn=_boom))
    provider = FakeProvider(
        _resp_tools(_tool_call("get_disk")),
        _resp_final("<final_answer>done</final_answer>"),
    )
    result = run_react_loop(provider=provider, registry=reg, route="kernel",
                            system_prompt="s", user_prompt="u")
    assert result.tool_trace[0]["error"] is True
    tool_msg = next(m for m in provider.requests[1].messages if m.role == "tool")
    assert tool_msg.content.startswith("error:")


def test_loop_insufficient_evidence():
    provider = FakeProvider(_resp_final("<insufficient_evidence> need a vmcore"))
    result = run_react_loop(provider=provider, registry=_registry(), route="kernel",
                            system_prompt="s", user_prompt="u")
    assert result.verdict == "insufficient_evidence"
    assert result.iterations == 1


# ── failure handling (T-003, ADR-019 D4) ────────────────────────────────────

def test_loop_repeated_tool_failure_exits_insufficient():
    """A tool stuck failing → insufficient_evidence after REPEAT_LIMIT tries."""
    def _boom():
        raise RuntimeError("evidence unreachable")

    reg = ToolRegistry()
    reg.register(Tool("get_x", "x", {"type": "object", "properties": {}}, fn=_boom))
    provider = FakeProvider(*[_resp_tools(_tool_call("get_x"))] * 10)
    result = run_react_loop(provider=provider, registry=reg, route="kernel",
                            system_prompt="s", user_prompt="u", max_iter=10)
    assert result.verdict == "insufficient_evidence"
    assert result.iterations == 3            # REPEAT_LIMIT


def test_loop_repeated_identical_call_exits_max_iter():
    """The LLM looping on the same successful call → max_iter_reached."""
    provider = FakeProvider(*[_resp_tools(_tool_call("get_meminfo"))] * 10)
    result = run_react_loop(provider=provider, registry=_registry(), route="kernel",
                            system_prompt="s", user_prompt="u", max_iter=10)
    assert result.verdict == "max_iter_reached"
    assert result.iterations == 3            # REPEAT_LIMIT, before max_iter


def test_loop_exits_on_token_budget():
    """Accumulated token usage over TOKEN_BUDGET → budget_exhausted (ADR-019 D3)."""
    big = ChatResponse(content="", finish_reason="tool_calls",
                       tool_calls=[_tool_call("get_meminfo")],
                       usage=Usage(input_tokens=30_000, output_tokens=5_000))
    provider = FakeProvider(big, big, big, big)   # 35K tokens per step
    result = run_react_loop(provider=provider, registry=_registry(), route="kernel",
                            system_prompt="s", user_prompt="u", max_iter=10)
    assert result.verdict == "budget_exhausted"
    assert result.iterations == 2                 # 70K > 50K budget after step 2
    assert result.tokens_used >= 50_000


# ── registry route subsetting (ADR-023) ─────────────────────────────────────

def test_registry_for_route_subsets():
    reg = _registry()
    assert {t.name for t in reg.for_route("kernel")} == {"get_meminfo"}
    assert {t.name for t in reg.for_route("hardware")} == {"get_mce_log"}


def test_registry_unrouted_tool_exposed_everywhere():
    reg = ToolRegistry()
    reg.register(Tool("parse_dmesg", "p", {"type": "object", "properties": {}},
                       fn=lambda: ""))
    assert len(reg.for_route("kernel")) == 1
    assert len(reg.for_route("hardware")) == 1


def test_registry_schemas_are_tool_schema():
    schemas = _registry().schemas("kernel")
    assert schemas[0].function.name == "get_meminfo"
    assert schemas[0].type == "function"
