"""Step-by-step driver for cases_smoke.json.

Each invocation runs ONE stage and persists state to
eval/results_v2/smoke/state.json so the next invocation can resume.
Stages:
  triage         parse_input + extract_events + taint/hw + classify + route
  retrieval      7-route BM25 seed retrieval
  react-init     build ReAct system+user prompt, list tools exposed for the route
  react-step     run ONE ReAct iteration (LLM call + tool dispatches)
  bind           bind_claims final pass
  report         render report_md / report_json

Run with --reset to start fresh.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

CASES_PATH = Path("eval/data/cases_smoke.json")
STATE_PATH = Path("eval/results_v2/smoke/state.json")
TRANSCRIPT_PATH = Path("eval/results_v2/smoke/transcript.md")


# ── Persistence ────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable = {}
    for k, v in state.items():
        try:
            json.dumps(v, default=str)
            serializable[k] = v
        except Exception:
            pass  # skip non-serializable
    STATE_PATH.write_text(json.dumps(serializable, indent=2, default=str))


def _append_transcript(title: str, body: str) -> None:
    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPT_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {title}\n\n{body}\n")


# ── Stage 1: triage ────────────────────────────────────────────────────────

def stage_triage() -> dict:
    case = json.loads(CASES_PATH.read_text(encoding="utf-8"))[0]
    state: dict = {"raw_input": case["raw_input"], "case_id": case["id"]}

    from agent.triage.nodes import (parse_input, extract_events,
                                     detect_taint_and_hw_signals,
                                     classify_fault_and_route)
    state = parse_input(state)
    state = extract_events(state)
    state = detect_taint_and_hw_signals(state)
    state = classify_fault_and_route(state)

    print("┌─ Stage 1 · TRIAGE ───────────────────────────────────────────")
    print(f"│  input_type        : {state['input_type']}")
    events = state.get("kernel_events", [])
    if events:
        print(f"│  events ({len(events)}):")
        for e in events[:5]:
            print(f"│    • [{e.get('kind'):<10}] {e.get('summary','')[:70]}")
    else:
        print(f"│  events            : (none extracted — will rely on LLM classify)")
    print(f"│  taint_flags       : {state.get('taint_flags', [])}")
    print(f"│  hardware_signals  : {state.get('hardware_signals', [])}")
    print(f"│  io_hang_signals   : {state.get('io_hang_signals', [])}")
    print(f"│  fault_kind        : {state['fault_kind']}")
    print(f"│  fault_summary     : {state.get('fault_summary','')[:70]}")
    print(f"│  sop_name          : {state['sop_name']}")
    print(f"│  diagnostic_route  : {state['diagnostic_route']}")
    print(f"└──────────────────────────────────────────────────────────────")

    _save_state(state)
    return state


# ── Stage 2: retrieval (7-route BM25 seed) ────────────────────────────────

def stage_retrieval() -> dict:
    state = _load_state()
    if not state.get("diagnostic_route"):
        raise SystemExit("Run --stage triage first.")

    from agent.triage.nodes import retrieve
    state = retrieve(state)
    evidence = state.get("evidence", [])

    print("┌─ Stage 2 · 7-ROUTE BM25 SEED RETRIEVAL ──────────────────────")
    print(f"│  parsed query     : {state.get('retrieval_query')}")
    print(f"│  total evidence   : {len(evidence)} rows")
    by_route: dict[str, int] = {}
    for e in evidence:
        by_route[e.get("route", "?")] = by_route.get(e.get("route", "?"), 0) + 1
    print(f"│  by route         : {by_route}")
    print(f"│  top 15 (by score):")
    for e in evidence[:15]:
        title = (e.get("title") or "").replace("\n", " ")[:70]
        h = (e.get("commit_hash") or "")[:12]
        h_part = f" [{h}]" if h else ""
        print(f"│    [{e.get('route','?'):<8}] {e.get('score',0):.2f}{h_part:<14}  {title}")
    print(f"└──────────────────────────────────────────────────────────────")

    _save_state(state)
    return state


# ── Stage 3: react-init ────────────────────────────────────────────────────

def stage_react_init() -> dict:
    state = _load_state()
    if "evidence" not in state:
        raise SystemExit("Run --stage retrieval first.")

    from agent.react.tools import build_registry
    from agent.react.prompts import render_system_prompt
    from agent.react.nodes import _build_user_prompt

    registry = build_registry()
    route = state.get("diagnostic_route", "unknown")

    system_prompt = render_system_prompt(route, state)
    user_prompt = _build_user_prompt(state)
    schemas = registry.schemas(route)

    print("┌─ Stage 3 · REACT INIT ───────────────────────────────────────")
    print(f"│  route exposed     : {route}")
    print(f"│  tools available   : {len(schemas)}")
    for s in schemas:
        print(f"│    • {s.function.name}")
    print(f"│  system_prompt len : {len(system_prompt):>6} chars")
    print(f"│  user_prompt len   : {len(user_prompt):>6} chars")
    print(f"└──────────────────────────────────────────────────────────────")
    print()
    print("─── system_prompt (first 1000 chars) ───")
    print(system_prompt[:1000])
    print("...")
    print()
    print("─── user_prompt (first 1500 chars) ───")
    print(user_prompt[:1500])

    # Store the prompts so react-step can resume cleanly
    state["_react_messages"] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    state["_react_route"] = route
    state["_react_tool_trace"] = []
    state["_react_tokens_used"] = 0
    state["_react_step"] = 0
    state["_react_seen_hashes"] = []
    state["_react_call_counts"] = {}  # key = f"{tool}|{args}"
    state["_react_fail_counts"] = {}

    _save_state(state)
    return state


# ── Stage 4: react-step (run one iteration) ───────────────────────────────

def stage_react_step() -> dict:
    state = _load_state()
    if "_react_messages" not in state:
        raise SystemExit("Run --stage react-init first.")

    import json as _json
    import re
    from llm.provider.base import ChatRequest, Message, ToolCall
    from llm.provider.registry import get_provider
    from agent.react.tools import build_registry
    from configs.config import get_config

    cfg = get_config()
    provider = get_provider(cfg.llm.chat.backend)
    registry = build_registry()
    route = state["_react_route"]
    schemas = registry.schemas(route)

    # Rehydrate messages
    raw_msgs = state["_react_messages"]
    messages = []
    for m in raw_msgs:
        tcs = m.get("tool_calls")
        if tcs:
            tcs = [ToolCall(**tc) for tc in tcs]
        messages.append(Message(
            role=m["role"], content=m.get("content"),
            tool_calls=tcs, tool_call_id=m.get("tool_call_id"),
            name=m.get("name"),
        ))

    call_counts = Counter()
    fail_counts = Counter()
    for k, v in state.get("_react_call_counts", {}).items():
        call_counts[k] = v
    for k, v in state.get("_react_fail_counts", {}).items():
        fail_counts[k] = v
    trace = state.get("_react_tool_trace", [])
    tokens_used = state.get("_react_tokens_used", 0)
    seen_hashes = state.get("_react_seen_hashes", [])
    seen_hash_set = set(h[:12] for h in seen_hashes)
    step = state.get("_react_step", 0) + 1

    from agent.react.loop import MAX_ITER, REPEAT_LIMIT, TOKEN_BUDGET
    force_finalize = (step >= MAX_ITER) or (tokens_used >= int(TOKEN_BUDGET * 0.85))
    tool_choice = "none" if force_finalize else "auto"

    print(f"┌─ Stage 4 · REACT STEP {step} / {MAX_ITER} "
          f"(tokens={tokens_used:,}/{TOKEN_BUDGET:,}, "
          f"force_finalize={force_finalize}) ──")

    resp = provider.chat(ChatRequest(
        messages=messages, tools=schemas, tool_choice=tool_choice,
        temperature=0.0, stream=False,
    ))
    tokens_used += resp.usage.input_tokens + resp.usage.output_tokens
    print(f"│  LLM call          : in={resp.usage.input_tokens} out={resp.usage.output_tokens}")

    content = resp.content or ""
    if (
        resp.finish_reason != "tool_calls"
        or not resp.tool_calls
        or "<final_answer>" in content
        or "<insufficient_evidence>" in content
    ):
        # Terminal step
        stripped = content.strip()
        if "<insufficient_evidence>" in content:
            verdict = "insufficient_evidence"
        elif "<final_answer>" in content and stripped:
            verdict = "diagnosed"
        else:
            verdict = "insufficient_evidence"
            if not stripped:
                content = "(empty content, no markers)"
        print(f"│  VERDICT: {verdict}")
        print(f"│")
        print(f"│  ── final content (first 2000 chars) ──")
        for line in content[:2000].splitlines():
            print(f"│  {line}")
        print(f"└──────────────────────────────────────────────────────────────")
        state["react_verdict"] = verdict
        state["react_final_answer"] = content
        state["final_analysis"] = content
        state["react_iterations"] = step
        state["react_tokens_used"] = tokens_used
        state["react_tool_trace"] = trace
        state["_react_step"] = step
        state["_react_done"] = True
        _save_state(state)
        return state

    # Tool calls — show + dispatch
    print(f"│  tool calls (×{len(resp.tool_calls)}):")
    messages.append(Message(role="assistant", content=resp.content or "",
                            tool_calls=resp.tool_calls))
    for tc in resp.tool_calls:
        name = tc.function.get("name", "")
        raw_args = tc.function.get("arguments") or "{}"
        key = f"{name}|{raw_args}"
        call_counts[key] += 1
        print(f"│    ▸ {name}({raw_args[:150]})")
        try:
            result = registry.dispatch(name, _json.loads(raw_args))
            content_r, errored = str(result), False
        except Exception as exc:  # noqa: BLE001
            content_r, errored = f"error: {exc}", True
            fail_counts[key] += 1
        preview = content_r[:300].replace("\n", " ")
        print(f"│       → {'ERR' if errored else 'OK '} ({len(content_r)} chars) "
              f"{preview[:200]}")
        messages.append(Message(role="tool", tool_call_id=tc.id,
                                name=name, content=content_r))
        trace.append({"step": step, "tool": name, "args": raw_args,
                      "error": errored, "result_preview": content_r[:300]})
        if not errored:
            for h in re.findall(r"\b([0-9a-f]{12,40})\b", content_r):
                hk = h[:12]
                if hk not in seen_hash_set:
                    seen_hash_set.add(hk)
                    seen_hashes.append(h)
    print(f"│  cumulative tools  : {len(trace)} total calls, {len(seen_hash_set)} unique commit hashes")
    print(f"└──────────────────────────────────────────────────────────────")

    # Persist updated runtime
    state["_react_messages"] = [m.model_dump(exclude_none=True) for m in messages]
    state["_react_call_counts"] = dict(call_counts)
    state["_react_fail_counts"] = dict(fail_counts)
    state["_react_tool_trace"] = trace
    state["_react_tokens_used"] = tokens_used
    state["_react_seen_hashes"] = seen_hashes
    state["_react_step"] = step
    state["_react_done"] = False
    _save_state(state)
    return state


# ── Stage 5: bind claims ───────────────────────────────────────────────────

def stage_bind() -> dict:
    state = _load_state()
    if not state.get("_react_done"):
        raise SystemExit("React loop not finished yet — run --stage react-step.")
    from agent.diagnosis.nodes import bind_claims

    # Restore evidence from react harvest before bind
    from agent.react.nodes import _merge_react_evidence
    state["evidence"] = _merge_react_evidence(
        existing=state.get("evidence", []),
        react_hashes=state.get("_react_seen_hashes", []),
    )
    state = bind_claims(state)
    print("┌─ Stage 5 · BIND CLAIMS ──────────────────────────────────────")
    print(f"│  groundedness        : {state.get('groundedness')}")
    print(f"│  verified_claims     : {len(state.get('verified_claims') or [])}")
    print(f"│  speculative_claims  : {len(state.get('speculative_claims') or [])}")
    print(f"│  claims sample (first 5):")
    for c in (state.get("claims") or [])[:5]:
        v = "✓" if c.get("verified") else "✗"
        print(f"│    {v} {(c.get('text','') or '')[:90]}")
    print(f"└──────────────────────────────────────────────────────────────")
    _save_state(state)
    return state


# ── Stage 6: report ───────────────────────────────────────────────────────

def stage_report() -> dict:
    state = _load_state()
    from agent.report.renderer import render_md
    md = render_md(state)
    print("┌─ Stage 6 · REPORT ───────────────────────────────────────────")
    print(f"│  report_md_length  : {len(md)} chars")
    print(f"│  ── first 2500 chars ──")
    for line in md[:2500].splitlines():
        print(f"│  {line}")
    print(f"└──────────────────────────────────────────────────────────────")
    state["report_md"] = md
    _save_state(state)
    Path("eval/results_v2/smoke/report.md").write_text(md, encoding="utf-8")
    return state


# ── Driver ─────────────────────────────────────────────────────────────────

STAGES = {
    "triage": stage_triage,
    "retrieval": stage_retrieval,
    "react-init": stage_react_init,
    "react-step": stage_react_step,
    "bind": stage_bind,
    "report": stage_report,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=list(STAGES), required=True)
    ap.add_argument("--reset", action="store_true",
                    help="Wipe persisted state before running")
    args = ap.parse_args()
    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        if TRANSCRIPT_PATH.exists():
            TRANSCRIPT_PATH.unlink()
        print("state reset.")
    STAGES[args.stage]()


if __name__ == "__main__":
    main()
