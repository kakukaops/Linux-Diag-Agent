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
TOKEN_BUDGET = 250_000  # ADR-019 D3 per-investigation cap. 50K v1 → 150K v2 (LKML
                        # backfill expanded context) → 200K post-ADR-022 (gitee/atomgit
                        # bug bodies 3-11KB each push tokens past 150K on 4/15 cases)
                        # → 250K post-v2.3 KG (panic-002 budget_exhausted in Run 11 at
                        # 219K; new KG tools surface more evidence per case so 200K is
                        # tight for symptom-class cases doing 12+ tool calls).


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
    # v2.3: structured evidence hashes extracted from tool outputs. The node
    # layer batch-fills titles from DB and merges into state["evidence"] so
    # bind_claims and recall@10 reflect what the agent actually found during
    # ReAct (not just the pre-ReAct BM25 triage retrieval).
    react_evidence_hashes: list[str] = field(default_factory=list)


def run_react_loop(*, provider, registry: ToolRegistry, route: str,
                   system_prompt: str, user_prompt: str,
                   max_iter: int = MAX_ITER,
                   on_event=None) -> ReactResult:
    """Run the ReAct investigation loop until a verdict is reached.

    `provider` is any object with a `.chat(ChatRequest) -> ChatResponse`
    method — i.e. the LLMProvider protocol from llm/provider/base.py.

    `on_event` (optional) is `Callable[[dict], None]` invoked at key points
    during the loop. Event types:
      {type:"step_start", step, tokens_used, force_finalize}
      {type:"tool_call", step, tool, args, errored, result_preview}
      {type:"terminate", verdict, content, iterations, tokens_used}
    Used by the web streaming pipeline; None in batch eval / CLI paths.
    """
    def _emit(ev: dict) -> None:
        if on_event is not None:
            try:
                on_event(ev)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_event callback raised: %s", exc)
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
    finalize_hallucination_retry = False  # one chance to recover from DSML
                                          # tool-call hallucinations under
                                          # tool_choice='none' (Bug #89).
    # v2.3: structured evidence (commit hashes) extracted from tool outputs.
    # Dedup while preserving first-seen order so the most-recently-found
    # candidates rank ahead of older ones.
    seen_hashes: list[str] = []
    seen_hash_set: set[str] = set()

    for step in range(1, max_iter + 1):
        # Force a final answer when we're near the budget — last iteration or
        # 85 % of the token cap. tool_choice="none" prevents the model from
        # calling more tools, so it has to produce text.
        force_finalize = (step == max_iter) or (tokens_used >= int(TOKEN_BUDGET * 0.85))
        if force_finalize and not finalize_reminder_added:
            messages.append(Message(
                role="user",
                content=(
                    "<<HARD CONSTRAINT — investigation budget exhausted>>\n"
                    "Tool calling is now DISABLED. Your next response will be "
                    "treated as the final report.\n\n"
                    "VALID OUTPUT — exactly ONE of these, plain text only "
                    "inside the tags:\n"
                    "  <final_answer>...your conclusion based on the evidence "
                    "already gathered above...</final_answer>\n"
                    "  <insufficient_evidence>...what additional data is "
                    "needed (vmcore / extra dmesg / kernel config)..."
                    "</insufficient_evidence>\n\n"
                    "FORBIDDEN — do NOT emit any of these in your output:\n"
                    "  • tool-call syntax of ANY format (no <tool_calls>, no "
                    "<｜｜DSML｜｜...>, no <invoke ...>, no JSON-RPC bodies)\n"
                    "  • requests for 'one more search' / 'one more lookup'\n"
                    "  • internal special tokens of any kind\n"
                    "Synthesise the answer from what is ALREADY in the "
                    "conversation. Re-querying the KG is not an option."
                ),
            ))
            finalize_reminder_added = True
        tool_choice = "none" if force_finalize else "auto"
        _emit({"type": "step_start", "step": step,
               "tokens_used": tokens_used,
               "force_finalize": force_finalize})
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
            # v2.3 fix: be strict about what counts as "diagnosed". Some
            # providers / safety filters return finish_reason='stop' with
            # empty or near-empty content. Previously this fell through to
            # verdict='diagnosed' with final_answer='', producing a blank
            # report and confusing downstream bind_claims (it saw analysis=""
            # → extracted 0 claims → groundedness='speculative' with 0/0
            # counts — surfaced by kasan-002 v2.3 smoke 2026-05-28).
            stripped = content.strip()

            # Bug #89: under force_finalize + tool_choice='none' some models
            # (DeepSeek-chat observed 2026-05-30 smoke) hallucinate tool-call
            # syntax in TEXT instead of emitting <final_answer>. The output
            # looks like '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke ...>'.
            # This bypasses our verdict tags and the answer is lost. Detect
            # the pattern and give the model ONE retry with a louder reminder.
            has_valid_tag = ("<final_answer>" in content
                             or "<insufficient_evidence>" in content)
            looks_like_tool_call_hallucination = stripped and (
                "DSML" in content
                or "<tool_calls>" in content
                or "<invoke " in content
                or "tool_call_id" in content
            )
            if (force_finalize
                    and looks_like_tool_call_hallucination
                    and not has_valid_tag
                    and not finalize_hallucination_retry):
                finalize_hallucination_retry = True
                messages.append(Message(role="assistant",
                                        content=resp.content or ""))
                messages.append(Message(
                    role="user",
                    content=(
                        "<<HARD RESET — your last reply was INVALID>>\n"
                        "You emitted tool-call syntax in the text body "
                        "(DSML / <tool_calls> / <invoke>). That is forbidden "
                        "under the budget-exhausted constraint.\n\n"
                        "Reply NOW with EXACTLY one block, nothing else:\n"
                        "  <final_answer>\n"
                        "  ## Root Cause\n"
                        "  ...one paragraph synthesised from the evidence "
                        "ALREADY in this conversation...\n\n"
                        "  ## Fix Recommendation\n"
                        "  ...one paragraph...\n\n"
                        "  ## Confidence\n"
                        "  high|medium|low — and why\n"
                        "  </final_answer>\n\n"
                        "If the evidence is truly insufficient, use "
                        "<insufficient_evidence>...</insufficient_evidence> "
                        "instead. NO tool calls. NO special tokens."
                    ),
                ))
                logger.info("react: force_finalize hallucination retry "
                            "(DSML detected at step %d)", step)
                continue

            if "<insufficient_evidence>" in content:
                verdict = "insufficient_evidence"
            elif "<final_answer>" in content and stripped:
                verdict = "diagnosed"
            else:
                # No closing tag AND/OR no substantive content — the LLM
                # exited without producing a defensible answer. Don't pretend
                # we diagnosed; surface honestly as insufficient.
                verdict = "insufficient_evidence"
                if not stripped:
                    content = ("LLM exited with empty content and no "
                               "final/insufficient marker — treated as "
                               "insufficient evidence.")
            _emit({"type": "terminate", "verdict": verdict,
                   "content": content, "iterations": step,
                   "tokens_used": tokens_used})
            return ReactResult(verdict, content, step, messages, trace,
                               tokens_used, list(seen_hashes))

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
                          "args": raw_args, "error": errored,
                          # v2.4: keep a short result preview so post-hoc
                          # process-compliance scoring can detect things like
                          # "did get_regression_fixes return a revert warning,
                          # and did the agent surface it in the final answer".
                          "result_preview": (content or "")[:300]})
            _emit({"type": "tool_call", "step": step, "tool": name,
                   "args": raw_args[:500], "errored": errored,
                   "result_preview": (content or "")[:500]})
            # v2.3: harvest commit hashes from this tool's output. Regex
            # matches both short (12) and full (40) lowercase hex hashes; we
            # store the 12-prefix as the canonical key (matches recall@10's
            # prefix-match logic in eval/runner_v2.py).
            if not errored:
                import re
                for h in re.findall(r"\b([0-9a-f]{12,40})\b", content):
                    hk = h[:12]
                    if hk not in seen_hash_set:
                        seen_hash_set.add(hk)
                        seen_hashes.append(h)

        # Failure handling (ADR-019 D4, M22 §4.3): a tool stuck failing means
        # the evidence isn't reachable → insufficient_evidence; the same call
        # repeated means the LLM is looping without progress → max_iter_reached.
        if max(fail_counts.values(), default=0) >= REPEAT_LIMIT:
            return ReactResult("insufficient_evidence", "", step,
                               messages, trace, tokens_used, list(seen_hashes))
        if max(call_counts.values(), default=0) >= REPEAT_LIMIT:
            return ReactResult("max_iter_reached", "", step,
                               messages, trace, tokens_used, list(seen_hashes))
        if tokens_used >= TOKEN_BUDGET:           # ADR-019 D3 cost ceiling
            return ReactResult("budget_exhausted", "", step,
                               messages, trace, tokens_used, list(seen_hashes))

        # TODO(M22 D5): checkpointer.save(state) after each step

    return ReactResult("max_iter_reached", "", max_iter, messages, trace,
                       tokens_used, list(seen_hashes))
