"""Streaming wrapper around the agent pipeline.

Yields events one at a time so the web UI can render the diagnosis as it
unfolds, instead of waiting for the whole pipeline to finish.

The event shape mirrors agent/graph.py:diagnose() stages:
  triage_input        — input type / raw input
  triage_events       — detected dmesg events
  triage_signals      — taint / hw / io signals
  triage_route        — fault_kind / sop / route
  retrieval           — seed BM25 evidence pool
  react_step_start    — entering ReAct iteration N
  react_tool_call     — one tool call dispatched in iteration N
  react_terminate     — verdict + final answer
  bind_claims         — claim grounding outcome
  report              — final report_md
  error               — exception during any stage
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


def stream_diagnose(raw_input: str, lang: str = "zh") -> Iterator[dict]:
    """Run the full pipeline, yielding events stage-by-stage.

    The ReAct loop is the long-running stage; it pushes events through an
    in-process Queue so we can yield them as they happen instead of waiting
    for the whole loop to finish.

    ``lang`` is appended to the ReAct system prompt as an output-language
    directive ('zh' or 'en'). Code identifiers stay verbatim either way.
    """
    try:
        yield from _run(raw_input, lang=lang)
    except Exception as exc:  # noqa: BLE001
        logger.exception("stream_diagnose crashed")
        yield {"type": "error",
               "stage": "pipeline",
               "message": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc(),
               }


def _run(raw_input: str, lang: str = "zh") -> Iterator[dict]:
    from agent.triage.nodes import (parse_input, extract_events,
                                     detect_taint_and_hw_signals,
                                     classify_fault_and_route, retrieve)

    state: dict = {"raw_input": raw_input}

    # ── Stage 1a: parse_input ────────────────────────────────────────────
    state = parse_input(state)
    yield {"type": "triage_input",
           "input_type": state.get("input_type"),
           "raw_input_len": len(raw_input)}

    # ── Stage 1b: extract_events ────────────────────────────────────────
    state = extract_events(state)
    events = state.get("kernel_events", []) or []
    yield {"type": "triage_events",
           "count": len(events),
           "events": [
               {"kind": e.get("kind"),
                "summary": (e.get("summary") or "")[:200]}
               for e in events[:5]
           ]}

    # ── Stage 1c: taint / hw / io signals ────────────────────────────────
    state = detect_taint_and_hw_signals(state)
    yield {"type": "triage_signals",
           "taint_flags": state.get("taint_flags", []),
           "hardware_signals": state.get("hardware_signals", []),
           "io_hang_signals": state.get("io_hang_signals", []),
           "has_hardware_signal": bool(state.get("has_hardware_signal"))}

    # ── Stage 1d: classify_fault_and_route ───────────────────────────────
    state = classify_fault_and_route(state)
    yield {"type": "triage_route",
           "fault_kind": state.get("fault_kind"),
           "fault_summary": (state.get("fault_summary") or "")[:300],
           "sop_name": state.get("sop_name"),
           "diagnostic_route": state.get("diagnostic_route")}

    # ── Stage 2: 7-route BM25 seed retrieval ─────────────────────────────
    state = retrieve(state)
    evidence = state.get("evidence", []) or []
    by_route: dict[str, int] = {}
    for e in evidence:
        r = str(e.get("route", "?"))
        by_route[r] = by_route.get(r, 0) + 1
    yield {"type": "retrieval",
           "total": len(evidence),
           "by_route": by_route,
           "top": [
               {"route": str(e.get("route", "?")),
                "score": round(float(e.get("score", 0) or 0), 3),
                "title": (e.get("title") or "")[:120],
                "commit_hash": (e.get("commit_hash") or "")[:12]}
               for e in evidence[:10]
           ]}

    # ── Stage 3: ReAct investigation (streamed via in-process Queue) ─────
    yield from _run_react_streamed(state, lang=lang)


def _run_react_streamed(state: dict, lang: str = "zh") -> Iterator[dict]:
    """Run react_investigation in a worker thread so the loop's on_event
    callbacks can be drained from a queue and yielded as SSE events while
    the loop is still running.
    """
    from llm.provider.registry import get_provider
    from configs.config import get_config
    from agent.react.loop import run_react_loop
    from agent.react.tools import build_registry
    from agent.react.prompts import render_system_prompt
    from agent.react.nodes import _build_user_prompt, _merge_react_evidence

    cfg = get_config()
    provider = get_provider(cfg.llm.chat.backend)
    registry = build_registry()
    route = state.get("diagnostic_route", "unknown")
    # Bilingual prompt: render_system_prompt(lang=zh) picks kernel.zh.md +
    # Chinese termination footer; lang=en uses the English originals.
    system_prompt = render_system_prompt(route, state, lang=lang)
    user_prompt = _build_user_prompt(state)

    yield {"type": "react_prep",
           "route": route,
           "tools_available": len(registry.schemas(route)),
           "system_prompt_len": len(system_prompt),
           "user_prompt_len": len(user_prompt),
           "lang": lang}

    q: queue.Queue = queue.Queue()
    sentinel = object()
    result_box: dict = {}

    def _on_event(ev: dict) -> None:
        q.put({"type": f"react_{ev['type']}", **{k: v for k, v in ev.items()
                                                  if k != 'type'}})

    def _worker() -> None:
        try:
            r = run_react_loop(provider=provider, registry=registry,
                               route=route, system_prompt=system_prompt,
                               user_prompt=user_prompt, on_event=_on_event)
            result_box["result"] = r
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = exc
        finally:
            q.put(sentinel)

    threading.Thread(target=_worker, daemon=True).start()

    # Drain events until sentinel
    while True:
        ev = q.get()
        if ev is sentinel:
            break
        yield ev

    if "error" in result_box:
        exc = result_box["error"]
        yield {"type": "error", "stage": "react",
               "message": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc()}
        return

    result = result_box["result"]

    # Merge react-harvested commit hashes into state.evidence so bind/report
    # see what the agent surfaced via search_commits / browse_subsystem_fixes.
    state["evidence"] = _merge_react_evidence(
        existing=state.get("evidence", []),
        react_hashes=result.react_evidence_hashes,
    )
    state["react_verdict"] = result.verdict
    state["react_final_answer"] = result.final_answer
    state["react_tool_trace"] = result.tool_trace
    state["react_iterations"] = result.iterations
    state["react_tokens_used"] = result.tokens_used
    state["final_analysis"] = result.final_answer

    # ── Stage 4: bind_claims ────────────────────────────────────────────
    yield from _run_bind_and_report(state, lang=lang)


def _run_bind_and_report(state: dict, lang: str = "zh") -> Iterator[dict]:
    from agent.diagnosis.nodes import bind_claims
    from agent.report.renderer import render_md

    state = bind_claims(state)
    yield {"type": "bind_claims",
           "groundedness": state.get("groundedness"),
           "verified_claims_n": len(state.get("verified_claims") or []),
           "speculative_claims_n": len(state.get("speculative_claims") or []),
           "claims": [
               {"text": (c.get("text") or "")[:160],
                "verified": bool(c.get("verified"))}
               for c in (state.get("claims") or [])[:8]
           ]}

    md = render_md(state, lang=lang)
    yield {"type": "report",
           "report_md_len": len(md),
           "report_md": md}
