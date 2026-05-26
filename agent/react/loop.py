"""M22 ReAct loop — observe → reason → tool_call → dispatch (T-003).

Runs a client-side ReAct loop directly on the project's LLM provider stack
(ChatRequest / ChatResponse / ToolCall), with NO dependency on LangChain's
`create_react_agent`. The M22 spike found `create_react_agent` requires a
LangChain `BaseChatModel`, which would mean abandoning the claude_code
backend, the PG prompt cache and the rate-limit guard — see
docs/v2/M22_spike_findings.md.

Implemented (T-003): the core loop, the iteration cap, failure handling (a
tool stuck failing or the same call repeated forces a verdict — ADR-019 D4,
M22 §4.3) and the token-budget guard (ADR-019 D3). NOT yet wired (TODO
below): per-step PG checkpoint (D5). The 50K budget is calibrated in T-026.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field

from agent.react.tool_registry import ToolRegistry
from llm.provider.base import ChatRequest, Message

logger = logging.getLogger(__name__)

MAX_ITER = 15           # ADR-019 D3 hard ceiling
REPEAT_LIMIT = 3        # ADR-019 D4: same (tool, args) repeated/failed N times → exit
TOKEN_BUDGET = 200_000  # ADR-019 D3 per-investigation cap. 50K v1 → 150K v2 (LKML
                        # backfill expanded context) → 200K post-ADR-022 (gitee/atomgit
                        # bug bodies 3-11KB each push tokens past 150K on 4/15 cases).


@dataclass
class ReactResult:
    """Outcome of one ReAct investigation."""

    # verdict ∈ {diagnosed, insufficient_evidence, max_iter_reached, budget_exhausted}
    verdict: str
    final_answer: str
    iterations: int
    messages: list[Message] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)
    tokens_used: int = 0


def run_react_loop(*, provider, registry: ToolRegistry, route: str,
                   system_prompt: str, user_prompt: str,
                   max_iter: int = MAX_ITER) -> ReactResult:
    """Run the ReAct investigation loop until a verdict is reached.

    `provider` is any object with a `.chat(ChatRequest) -> ChatResponse`
    method — i.e. the LLMProvider protocol from llm/provider/base.py.
    """
    messages: list[Message] = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
    schemas = registry.schemas(route)
    trace: list[dict] = []
    call_counts: Counter = Counter()   # (tool, args) → times called
    fail_counts: Counter = Counter()   # (tool, args) → times it errored
    tokens_used = 0
    finalize_reminder_added = False

    for step in range(1, max_iter + 1):
        # Force a final answer when we're near the budget — last iteration or
        # 85 % of the token cap. tool_choice="none" prevents the model from
        # calling more tools, so it has to produce text.
        force_finalize = (step == max_iter) or (tokens_used >= int(TOKEN_BUDGET * 0.85))
        if force_finalize and not finalize_reminder_added:
            messages.append(Message(
                role="user",
                content=(
                    "<<system reminder>>: Investigation budget reached. "
                    "Produce your final answer now using ONLY the evidence "
                    "already gathered. Wrap it in <final_answer> or "
                    "<insufficient_evidence>. Do NOT call any tools."
                ),
            ))
            finalize_reminder_added = True
        tool_choice = "none" if force_finalize else "auto"
        resp = provider.chat(ChatRequest(
            messages=messages, tools=schemas, tool_choice=tool_choice,
            temperature=0.0, stream=False,
        ))
        tokens_used += resp.usage.input_tokens + resp.usage.output_tokens

        # Terminate when: (a) no tool_calls, or (b) the content already
        # carries a final/insufficient block — some models emit both in one
        # turn; honour the answer and stop.
        content = resp.content or ""
        if (
            resp.finish_reason != "tool_calls"
            or not resp.tool_calls
            or "<final_answer>" in content
            or "<insufficient_evidence>" in content
        ):
            verdict = ("insufficient_evidence"
                       if "<insufficient_evidence>" in content else "diagnosed")
            return ReactResult(verdict, content, step, messages, trace, tokens_used)

        # Tool calls requested → dispatch each, thread results back in.
        messages.append(Message(role="assistant", content=resp.content or "",
                                tool_calls=resp.tool_calls))
        for tc in resp.tool_calls:
            name = tc.function.get("name", "")
            raw_args = tc.function.get("arguments") or "{}"
            key = (name, raw_args)
            call_counts[key] += 1
            try:
                result = registry.dispatch(name, json.loads(raw_args))
                content, errored = str(result), False
            except Exception as exc:                       # noqa: BLE001
                content, errored = f"error: {exc}", True
                fail_counts[key] += 1
                logger.warning("react tool %s failed: %s", name, exc)
            messages.append(Message(role="tool", tool_call_id=tc.id,
                                    name=name, content=content))
            trace.append({"step": step, "tool": name,
                          "args": raw_args, "error": errored})

        # Failure handling (ADR-019 D4, M22 §4.3): a tool stuck failing means
        # the evidence isn't reachable → insufficient_evidence; the same call
        # repeated means the LLM is looping without progress → max_iter_reached.
        if max(fail_counts.values(), default=0) >= REPEAT_LIMIT:
            return ReactResult("insufficient_evidence", "", step,
                               messages, trace, tokens_used)
        if max(call_counts.values(), default=0) >= REPEAT_LIMIT:
            return ReactResult("max_iter_reached", "", step,
                               messages, trace, tokens_used)
        if tokens_used >= TOKEN_BUDGET:           # ADR-019 D3 cost ceiling
            return ReactResult("budget_exhausted", "", step,
                               messages, trace, tokens_used)

        # TODO(M22 D5): checkpointer.save(state) after each step

    return ReactResult("max_iter_reached", "", max_iter, messages, trace, tokens_used)
