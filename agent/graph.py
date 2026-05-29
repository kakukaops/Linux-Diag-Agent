"""LangGraph diagnosis agent graph (v2 T-005).

Three-phase Hybrid Agent (ADR-019):
  Triage  (deterministic): parse_input → extract_events → detect_taint_and_hw_signals
                           → classify_fault_and_route → retrieve
  Investigation (ReAct):   react_investigation   (M22 loop replacing v1 load_sop..self_consistency)
  Report  (deterministic): bind_claims → generate_report

Checkpointing uses the PG checkpointer (WBS 7.12).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from agent.triage.nodes import (
    parse_input,
    extract_events,
    detect_taint_and_hw_signals,
    classify_fault_and_route,
    retrieve,
)
from agent.react.nodes import react_investigation
from agent.diagnosis.nodes import bind_claims, generate_report
from agent.dmesg_event_writer import write_dmesg_event


def build_graph() -> Any:
    """Build and compile the full triage + ReAct + report StateGraph."""
    graph = StateGraph(dict)

    # ── Triage phase (deterministic) ─────────────────────────────────────────
    graph.add_node("parse_input", parse_input)
    graph.add_node("extract_events", extract_events)
    graph.add_node("detect_taint_and_hw_signals", detect_taint_and_hw_signals)
    graph.add_node("classify_fault_and_route", classify_fault_and_route)
    graph.add_node("retrieve", retrieve)

    # ── Investigation phase (ReAct — M22) ────────────────────────────────────
    graph.add_node("react_investigation", react_investigation)

    # ── Report phase (deterministic) ─────────────────────────────────────────
    graph.add_node("bind_claims", bind_claims)
    graph.add_node("generate_report", generate_report)
    # v2.4 functional-closure: persist this session as a dmesg_event so
    # future runs of find_similar_crashes can match against it.
    graph.add_node("write_dmesg_event", write_dmesg_event)

    # ── Edges ────────────────────────────────────────────────────────────────
    graph.set_entry_point("parse_input")
    graph.add_edge("parse_input", "extract_events")
    graph.add_edge("extract_events", "detect_taint_and_hw_signals")
    graph.add_edge("detect_taint_and_hw_signals", "classify_fault_and_route")
    graph.add_edge("classify_fault_and_route", "retrieve")
    graph.add_edge("retrieve", "react_investigation")
    graph.add_edge("react_investigation", "bind_claims")
    graph.add_edge("bind_claims", "generate_report")
    graph.add_edge("generate_report", "write_dmesg_event")
    graph.add_edge("write_dmesg_event", END)

    # Wire PG checkpointer if available (WBS 7.12)
    try:
        from agent.checkpointer import get_checkpointer
        checkpointer = get_checkpointer()
        return graph.compile(checkpointer=checkpointer)
    except Exception:
        return graph.compile()


# ── Convenience runner ────────────────────────────────────────────────────────


def diagnose(raw_input: str, *, thread_id: str | None = None) -> dict[str, Any]:
    """Run the full diagnosis pipeline on raw_input. Returns final state."""
    import uuid
    app = build_graph()
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    initial_state = {
        "raw_input": raw_input,
        "messages": [],
        "iteration": 0,
    }
    return app.invoke(initial_state, config)
