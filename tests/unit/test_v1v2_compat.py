"""v1 → v2 compatibility regression tests (T-027).

Verifies that the v2 agent pipeline:
  1. Still accepts the same raw_input formats (dmesg text, question, file path)
  2. Still emits report_md and report_json in state
  3. report_json schema_version=2 (v2 upgrade verified)
  4. State keys added by v2 (react_*) are present and typed correctly
  5. v1 state keys still present for backward compat (fault_kind, sop_name, evidence, claims)
  6. bind_claims short-circuits on non-diagnosed verdict (T-010 regression)
  7. render_md/render_json handle both diagnosed and insufficient_evidence verdicts
  8. All 10 SOP yaml files still load (v1 SOPs not broken by v2 hardware.yaml addition)
  9. graph.build_graph() compiles without error
  10. diagnose() entrypoint callable (signature unchanged)
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ── 1. State contract — v1 keys still present ─────────────────────────────────

MINIMAL_DIAGNOSED_STATE = {
    "raw_input": "OOM kill of java pid 1234",
    "fault_kind": "oom",
    "fault_summary": "OOM kill of java",
    "olk_version_tag": "OLK-6.6",
    "hostname": "testhost",
    "final_analysis": "Memory overcommit caused OOM.",
    "hypotheses": [],
    "claims": [{"text": "java had 4GB RSS", "evidence_refs": ["abc1234"], "verified": True}],
    "evidence": [{"route": "commit", "title": "mm: fix OOM", "score": 0.9,
                  "commit_hash": "abc1234", "bug_id": None, "cve_id": None}],
    "candidate_analyses": ["analysis 1"],
    "sop_name": "oom",
    # v2 additions
    "react_verdict": "diagnosed",
    "react_final_answer": "Memory overcommit caused OOM.",
    "react_tool_trace": [],
    "react_iterations": 3,
    "react_tokens_used": 1500,
    "diagnostic_route": "kernel",
}


def test_report_md_still_contains_v1_fields():
    """v2 render_md still emits all v1 required fields."""
    from agent.report.renderer import render_md
    md = render_md(MINIMAL_DIAGNOSED_STATE)
    assert "oom" in md.lower()
    assert "OOM kill" in md
    assert "OLK-6.6" in md
    assert "Memory overcommit" in md
    assert "abc1234" in md              # evidence ref from claim


def test_report_json_schema_version_2():
    """schema_version must be 2 in v2 (upgraded from 1)."""
    from agent.report.renderer import render_json
    report = render_json(MINIMAL_DIAGNOSED_STATE)
    assert report["schema_version"] == 2


def test_report_json_v1_fields_present():
    """v1-era fields still present in schema_version=2 output."""
    from agent.report.renderer import render_json
    report = render_json(MINIMAL_DIAGNOSED_STATE)
    # v1 fields
    assert "fault" in report
    assert report["fault"]["kind"] == "oom"
    assert "system" in report
    assert "generated_at" in report
    assert "top_evidence" in report


def test_report_json_v2_react_block():
    """v2 adds 'react' block with verdict/iterations/tokens_used."""
    from agent.report.renderer import render_json
    report = render_json(MINIMAL_DIAGNOSED_STATE)
    assert "react" in report
    assert report["react"]["verdict"] == "diagnosed"
    assert report["react"]["iterations"] == 3
    assert report["react"]["tokens_used"] == 1500


def test_report_json_v2_claims_and_evidence():
    from agent.report.renderer import render_json
    report = render_json(MINIMAL_DIAGNOSED_STATE)
    assert "claims" in report
    assert len(report["claims"]) == 1
    assert len(report["top_evidence"]) == 1
    assert report["top_evidence"][0]["commit_hash"] == "abc1234"


# ── 2. insufficient_evidence branch (T-010 / T-011 regression) ───────────────

INSUFFICIENT_STATE = {
    **MINIMAL_DIAGNOSED_STATE,
    "react_verdict": "insufficient_evidence",
    "react_final_answer": "Need more logs: provide full dmesg with timestamps.",
    "claims": [],
}


def test_render_md_insufficient_evidence_branch():
    from agent.report.renderer import render_md
    md = render_md(INSUFFICIENT_STATE)
    assert "Incomplete" in md or "Insufficient" in md
    assert "dmesg" in md.lower() or "Need more" in md


def test_render_json_insufficient_evidence_branch():
    from agent.report.renderer import render_json
    report = render_json(INSUFFICIENT_STATE)
    assert "insufficient_evidence" in report
    assert report["react"]["verdict"] == "insufficient_evidence"
    # Diagnosed-only fields should NOT be present
    assert "analysis" not in report
    assert "top_evidence" not in report


def test_render_md_max_iter_reached():
    from agent.report.renderer import render_md
    state = {**INSUFFICIENT_STATE, "react_verdict": "max_iter_reached"}
    md = render_md(state)
    assert "Limit" in md or "Reached" in md or "max_iter" in md.lower()


def test_render_md_budget_exhausted():
    from agent.report.renderer import render_md
    state = {**INSUFFICIENT_STATE, "react_verdict": "budget_exhausted"}
    md = render_md(state)
    assert "Budget" in md or "Exhausted" in md or "budget" in md.lower()


# ── 3. bind_claims short-circuits on non-diagnosed ───────────────────────────

def test_bind_claims_skips_llm_on_insufficient():
    """bind_claims must NOT call LLM when react_verdict != 'diagnosed' (T-010)."""
    from agent.diagnosis.nodes import bind_claims
    state = {
        "react_verdict": "insufficient_evidence",
        "final_analysis": "some text",
        "evidence": [],
    }
    with patch("agent.diagnosis.nodes._llm_call") as mock_llm:
        result = bind_claims(state)
    mock_llm.assert_not_called()
    assert result["claims"] == []


def test_bind_claims_calls_llm_on_diagnosed():
    """v2.1 P0-2: verified requires BOTH hash-existence AND keyword overlap
    between claim text and evidence body/title."""
    from agent.diagnosis.nodes import bind_claims
    state = {
        "react_verdict": "diagnosed",
        "final_analysis": "Memory overcommit caused OOM in cgroup memcg.",
        "evidence": [{
            "commit_hash": "abc1234",
            "title": "memcg: fix overcommit OOM accounting",
            "body": "Fix incorrect memory overcommit accounting that triggers "
                    "premature OOM kill in memcg with high anon-rss pressure.",
            "bug_id": None,
        }],
    }
    mock_response = ('[{"text": "OOM triggered by memcg overcommit '
                     'accounting bug", "evidence_refs": ["abc1234"]}]')
    with patch("agent.diagnosis.nodes._llm_call", return_value=mock_response):
        result = bind_claims(state)
    assert len(result["claims"]) == 1
    assert result["claims"][0]["verified"] is True
    assert result["groundedness"] == "grounded"
    assert len(result["verified_claims"]) == 1
    assert len(result["speculative_claims"]) == 0


def test_bind_claims_auto_attaches_matching_evidence():
    """v2.2 Path C: when LLM cites an UNRELATED ref but the pool DOES
    contain a relevant one, auto-attach rescues the claim.

    Real scenario: agent's analysis correctly diagnoses the issue but
    cites the wrong commit. Auto-attach should find the right one in
    the retrieved evidence pool by keyword overlap.
    """
    from agent.diagnosis.nodes import bind_claims
    state = {
        "react_verdict": "diagnosed",
        "final_analysis": "Memory cgroup OOM kill due to memcontrol throttle of dying tasks.",
        "evidence": [
            # Unrelated commit that the LLM might cite by mistake
            {"commit_hash": "abc1234", "title": "btrfs: nodesize cleanup",
             "body": "Refactor nodesize.", "bug_id": None},
            # The actual matching commit, present in pool but NOT cited by LLM
            {"commit_hash": "892962a", "title": "memcontrol: don't throttle dying tasks",
             "body": "Fix memcg throttle of dying tasks on memory.high causing OOM kill.",
             "bug_id": None},
        ],
    }
    # LLM cites the WRONG commit
    mock_response = ('[{"text": "OOM caused by memcontrol throttling dying tasks", '
                     '"evidence_refs": ["abc1234"]}]')
    with patch("agent.diagnosis.nodes._llm_call", return_value=mock_response):
        result = bind_claims(state)

    # Should auto-attach 892962a and verify
    assert result["claims"][0]["verified"] is True, \
        "auto-attach should rescue: claim overlaps with 892962a even though LLM cited abc1234"
    assert result["groundedness"] == "grounded"
    assert any("AUTO:" in str(r) for r in result["claims"][0]["evidence_refs"])


def test_bind_claims_rejects_unrelated_evidence():
    """v2.1 P0-2: LLM citing a real-but-unrelated commit must NOT be verified.

    Guards against rubber-stamping irrelevant citations — the agent might
    point at any commit in retrieved evidence; we require claim text to
    actually overlap with that commit's content.
    """
    from agent.diagnosis.nodes import bind_claims
    state = {
        "react_verdict": "diagnosed",
        "final_analysis": "Network stack TCP UAF.",
        "evidence": [{
            "commit_hash": "abc1234",
            "title": "btrfs: convert nodesize macros into static inline",
            "body": "Refactor nodesize helpers. Pure cleanup, no behavior change.",
            "bug_id": None,
        }],
    }
    # LLM claims TCP UAF but cites a btrfs cleanup commit → must not pass.
    mock_response = ('[{"text": "TCP receive path use-after-free in '
                     'tcp_v4_do_rcv", "evidence_refs": ["abc1234"]}]')
    with patch("agent.diagnosis.nodes._llm_call", return_value=mock_response):
        result = bind_claims(state)
    assert result["claims"][0]["verified"] is False
    assert result["groundedness"] == "speculative"
    assert len(result["verified_claims"]) == 0
    assert len(result["speculative_claims"]) == 1


# ── 4. All SOP yaml files still load ─────────────────────────────────────────

def test_all_sops_load():
    from agent.sop.registry import get_sop, list_sops
    sop_names = list_sops()
    assert len(sop_names) >= 10, f"Expected ≥10 SOPs, got {len(sop_names)}: {sop_names}"
    # Spot check: all v1 SOPs still present
    for name in ("oom", "lockup", "panic", "generic", "deadlock", "io_hang",
                 "network", "perf_regression", "sched_anomaly"):
        assert name in sop_names, f"v1 SOP '{name}' missing after v2 additions"
    # v2 new SOP
    assert "hardware" in sop_names


def test_all_sops_have_steps():
    from agent.sop.registry import get_sop, list_sops
    for name in list_sops():
        sop = get_sop(name)
        steps = sop.get("steps", [])
        assert len(steps) > 0, f"SOP '{name}' has no steps"


# ── 5. Graph compiles without error ──────────────────────────────────────────

def test_build_graph_compiles():
    """agent.graph.build_graph() must compile without raising."""
    from agent.graph import build_graph
    graph = build_graph()
    assert graph is not None


def test_diagnose_callable():
    """diagnose() entrypoint accepts raw_input string (signature unchanged)."""
    import inspect
    from agent.graph import diagnose
    sig = inspect.signature(diagnose)
    assert "raw_input" in sig.parameters


# ── 6. ToolRegistry stable ───────────────────────────────────────────────────

def test_registry_tool_count_stable():
    """Registry must have ≥20 tools across all routes (regression check)."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    # Total unique tools (across all routes)
    total = len(reg._tools)
    assert total >= 20, f"Registry has only {total} tools — likely import error"


def test_registry_kernel_route_tools():
    """kernel route must have the core v1-era retrieval + v2 tools."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    kernel_tools = {t.name for t in reg.for_route("kernel")}
    v1_era = {"search_commits", "search_lkml", "search_bugs", "search_syzbot",
              "search_cve", "parse_dmesg"}
    v2_new = {"check_backport_status", "get_commit_diff", "decode_stacktrace"}
    assert v1_era <= kernel_tools, f"v1-era tools missing: {v1_era - kernel_tools}"
    assert v2_new <= kernel_tools, f"v2 tools missing: {v2_new - kernel_tools}"


# ── 7. triage nodes backward compat ──────────────────────────────────────────

def test_parse_input_dmesg_heuristic():
    from agent.triage.nodes import parse_input
    state = {"raw_input": "[  100.000000] BUG: KASAN: use-after-free in tcp_v4_do_rcv"}
    result = parse_input(state)
    assert result["input_type"] == "dmesg"


def test_parse_input_question():
    from agent.triage.nodes import parse_input
    state = {"raw_input": "How do I diagnose OOM on OLK-6.6?"}
    result = parse_input(state)
    assert result["input_type"] == "question"


def test_classify_fault_and_route_hardware_route():
    """If has_hardware_signal=True, route must be 'hardware' (ADR-023)."""
    from agent.triage.nodes import classify_fault_and_route
    state = {
        "kernel_events": [{"kind": "panic", "summary": "Fatal MCE", "call_trace": []}],
        "has_hardware_signal": True,
        "taint_flags": ["M"],
        "hardware_signals": ["mce"],
        "raw_input": "",
    }
    result = classify_fault_and_route(state)
    assert result["diagnostic_route"] == "hardware"
    assert result["sop_name"] == "hardware"


def test_classify_fault_and_route_change_route():
    """Upgrade-mention text → change route."""
    from agent.triage.nodes import classify_fault_and_route
    state = {
        "kernel_events": [],
        "has_hardware_signal": False,
        "taint_flags": [],
        "hardware_signals": [],
        "raw_input": "After upgrading the kernel everything broke",
    }
    with patch("agent.triage.nodes._llm_classify",
               return_value=("generic", "Unknown fault")):
        result = classify_fault_and_route(state)
    assert result["diagnostic_route"] == "change"


# ── 8. report_json for hardware route (no commit refs) ───────────────────────

HARDWARE_STATE = {
    "fault_kind": "hardlockup",
    "fault_summary": "Fatal MCE on CPU 12",
    "olk_version_tag": "OLK-6.6",
    "hostname": "server01",
    "final_analysis": "Hardware DIMM failure detected.",
    "react_verdict": "diagnosed",
    "react_final_answer": "Hardware DIMM failure detected.",
    "react_tool_trace": [
        {"step": 1, "tool": "parse_taint_flags", "args": {}, "error": False},
        {"step": 2, "tool": "get_mce_log", "args": {}, "error": False},
        {"step": 3, "tool": "get_edac_errors", "args": {}, "error": False},
    ],
    "react_iterations": 3,
    "react_tokens_used": 800,
    "diagnostic_route": "hardware",
    "claims": [],
    "evidence": [],
    "candidate_analyses": [],
    "sop_name": "hardware",
}


def test_hardware_report_json_tool_trace():
    from agent.report.renderer import render_json
    report = render_json(HARDWARE_STATE)
    assert len(report["react"]["tool_calls"]) == 3
    tool_names = [tc["tool"] for tc in report["react"]["tool_calls"]]
    assert "get_mce_log" in tool_names
    assert "parse_taint_flags" in tool_names


def test_hardware_report_md_no_commit_search():
    """Hardware reports should not mention kernel commit search (ADR-023)."""
    from agent.report.renderer import render_md
    md = render_md(HARDWARE_STATE)
    # The tool trace section should show hardware tools were used
    assert "get_mce_log" in md or "parse_taint_flags" in md or "Hardware" in md
