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

def test_loop_harvests_commit_hashes_from_tool_output():
    """v2.3: hashes mentioned in tool results land in react_evidence_hashes
    so the node layer can merge them into state.evidence for bind_claims +
    recall@10."""
    reg = ToolRegistry()
    reg.register(Tool(
        "search_commits", "search", {"type": "object", "properties": {}},
        fn=lambda **kw: (
            "[1] score=0.80 | net: fix crash | hash=9ab5cf19fb0e\n"
            "[2] score=0.70 | net: another | hash=8615d2e1fb7d\n"
        ),
    ))
    provider = FakeProvider(
        _resp_tools(_tool_call("search_commits")),
        _resp_final("<final_answer>fixed by 9ab5cf19fb0e</final_answer>"),
    )
    result = run_react_loop(provider=provider, registry=reg, route="kernel",
                            system_prompt="s", user_prompt="u")
    assert result.verdict == "diagnosed"
    keys = {h[:12] for h in result.react_evidence_hashes}
    assert "9ab5cf19fb0e" in keys
    assert "8615d2e1fb7d" in keys


def test_loop_evidence_hashes_dedup():
    """Same hash returned by two tools = one entry."""
    reg = ToolRegistry()
    reg.register(Tool("a", "x", {"type": "object", "properties": {}},
                       fn=lambda **kw: "hash=abc123def456"))
    reg.register(Tool("b", "y", {"type": "object", "properties": {}},
                       fn=lambda **kw: "look at abc123def456 again"))
    provider = FakeProvider(
        _resp_tools(_tool_call("a"), _tool_call("b")),
        _resp_final("<final_answer>done</final_answer>"),
    )
    result = run_react_loop(provider=provider, registry=reg, route="kernel",
                            system_prompt="s", user_prompt="u")
    assert len(result.react_evidence_hashes) == 1
    assert result.react_evidence_hashes[0].startswith("abc123def456")


def test_loop_errored_tool_does_not_harvest():
    """Errored tool output must not contribute fake hashes."""
    def _boom():
        raise RuntimeError("see hash=deadbeefcafe somewhere")
    reg = ToolRegistry()
    reg.register(Tool("b", "x", {"type": "object", "properties": {}}, fn=_boom))
    provider = FakeProvider(
        _resp_tools(_tool_call("b")),
        _resp_final("<final_answer>done</final_answer>"),
    )
    result = run_react_loop(provider=provider, registry=reg, route="kernel",
                            system_prompt="s", user_prompt="u")
    assert result.react_evidence_hashes == []


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
    """Accumulated token usage over TOKEN_BUDGET → budget_exhausted (ADR-019 D3).

    Per-step tokens calibrated to TOKEN_BUDGET so the test is robust to the
    constant changing (50K → 150K → 200K across v2 evolution).
    """
    from agent.react.loop import TOKEN_BUDGET
    # Per-step ~60 % of budget → step 2 crosses TOKEN_BUDGET (avoids tripping
    # REPEAT_LIMIT=3 with same-tool-call provider).
    per_step = int(TOKEN_BUDGET * 0.6)
    big = ChatResponse(content="", finish_reason="tool_calls",
                       tool_calls=[_tool_call("get_meminfo")],
                       usage=Usage(input_tokens=per_step - 5_000,
                                   output_tokens=5_000))
    provider = FakeProvider(*[big] * 10)
    result = run_react_loop(provider=provider, registry=_registry(), route="kernel",
                            system_prompt="s", user_prompt="u", max_iter=10)
    assert result.verdict == "budget_exhausted"
    assert result.iterations == 2
    assert result.tokens_used >= TOKEN_BUDGET


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
