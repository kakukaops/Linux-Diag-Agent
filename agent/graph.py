"""LangGraph diagnosis agent graph (WBS 7.1/7.2).

Two-phase pipeline:
  Triage phase: parse_input → extract_events → classify_fault → retrieve
  Diagnosis phase: load_sop → generate_hypotheses → verify_hypothesis →
                   self_consistency → bind_claims → generate_report

Checkpointing falls to PG via SqliteSaver as an interim; PG checkpointer
is wired in WBS 7.12.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from agent.triage.nodes import parse_input, extract_events, classify_fault, retrieve
from agent.diagnosis.nodes import (
    load_sop,
    generate_hypotheses,
    verify_hypothesis,
    self_consistency,
    bind_claims,
    generate_report,
)


def _merge_states(a: dict, b: dict) -> dict:
    """Simple merge: b overrides a."""
    return {**a, **b}


def build_graph() -> Any:
    """Build and compile the full triage + diagnosis StateGraph."""
    # Combined state is the union of TriageState and DiagnosisState
    graph = StateGraph(dict)

    # ── Triage phase ─────────────────────────────────────────────────────────
    graph.add_node("parse_input", parse_input)
    graph.add_node("extract_events", extract_events)
    graph.add_node("classify_fault", classify_fault)
    graph.add_node("retrieve", retrieve)

    # ── Diagnosis phase ───────────────────────────────────────────────────────
    graph.add_node("load_sop", load_sop)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("verify_hypothesis", verify_hypothesis)
    graph.add_node("self_consistency", self_consistency)
    graph.add_node("bind_claims", bind_claims)
    graph.add_node("generate_report", generate_report)

    # ── Edges ────────────────────────────────────────────────────────────────
    graph.set_entry_point("parse_input")
    graph.add_edge("parse_input", "extract_events")
    graph.add_edge("extract_events", "classify_fault")
    graph.add_edge("classify_fault", "retrieve")
    graph.add_edge("retrieve", "load_sop")
    graph.add_edge("load_sop", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "verify_hypothesis")
    graph.add_edge("verify_hypothesis", "self_consistency")
    graph.add_edge("self_consistency", "bind_claims")
    graph.add_edge("bind_claims", "generate_report")
    graph.add_edge("generate_report", END)

    return graph.compile()


# ── Convenience runner ────────────────────────────────────────────────────────


def diagnose(raw_input: str) -> dict[str, Any]:
    """Run the full diagnosis pipeline on raw_input. Returns final state."""
    app = build_graph()
    initial_state = {
        "raw_input": raw_input,
        "messages": [],
        "iteration": 0,
    }
    return app.invoke(initial_state)
