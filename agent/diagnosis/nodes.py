"""LangGraph diagnosis nodes (WBS 7.2).

Nodes:
  load_sop          → load SOP yaml steps
  generate_hypotheses → LLM generates K hypotheses from evidence
  verify_hypothesis   → LLM verifies top hypothesis against evidence
  self_consistency    → K=3 LLM analyses, majority vote (WBS 7.8)
  bind_claims         → extract claims and verify evidence refs (WBS 7.9)
  generate_report     → render Markdown + JSON report (WBS 7.10)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.sop.registry import get_sop

logger = logging.getLogger(__name__)

_K = 3  # self-consistency samples


# ── Node implementations ──────────────────────────────────────────────────────


def load_sop(state: dict) -> dict:
    """Load SOP yaml steps for the detected fault kind."""
    sop_name = state.get("sop_name", "generic")
    try:
        sop = get_sop(sop_name)
        steps = sop.get("steps", [])
    except Exception as exc:
        logger.warning("SOP '%s' not found, using generic: %s", sop_name, exc)
        sop = get_sop("generic")
        steps = sop.get("steps", [])
    return {**state, "sop_steps": steps, "current_step": 0}


def generate_hypotheses(state: dict) -> dict:
    """LLM generates hypotheses for the fault based on evidence."""
    evidence = state.get("evidence", [])
    fault_summary = state.get("fault_summary", "")
    sop_steps = state.get("sop_steps", [])

    evidence_text = "\n".join(
        f"- [{e['route']}] {e.get('title','')} : {e.get('body','')[:200]}"
        for e in evidence[:10]
    )
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sop_steps[:5]))

    prompt = (
        f"Fault: {fault_summary}\n\n"
        f"SOP steps:\n{steps_text}\n\n"
        f"Evidence:\n{evidence_text}\n\n"
        "Generate 3 hypotheses explaining this kernel fault. "
        'Return JSON array: [{"id":"h1","text":"...","confidence":0.8}]'
    )
    raw = _llm_call(prompt)
    try:
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        hyps_data: list[dict] = json.loads(raw_clean)
    except Exception:
        hyps_data = [{"id": "h1", "text": fault_summary, "confidence": 0.5}]

    hypotheses: list[Hypothesis] = [
        {
            "id": h.get("id", f"h{i}"),
            "text": h.get("text", ""),
            "confidence": float(h.get("confidence", 0.5)),
            "supporting_evidence": [],
            "status": "pending",
        }
        for i, h in enumerate(hyps_data)
    ]
    hypotheses.sort(key=lambda h: h["confidence"], reverse=True)
    active = hypotheses[0] if hypotheses else None
    return {**state, "hypotheses": hypotheses, "active_hypothesis": active}


def verify_hypothesis(state: dict) -> dict:
    """Verify the active hypothesis against evidence; update status."""
    hyp = state.get("active_hypothesis")
    if not hyp:
        return state
    evidence = state.get("evidence", [])

    evidence_text = "\n".join(
        f"- [{e['route']}] {e.get('title','')} : {e.get('body','')[:200]}"
        for e in evidence[:8]
    )
    prompt = (
        f"Hypothesis: {hyp['text']}\n\n"
        f"Evidence:\n{evidence_text}\n\n"
        "Is this hypothesis supported by the evidence? "
        'Return JSON: {"status":"confirmed|rejected|uncertain","reasoning":"..."}'
    )
    raw = _llm_call(prompt)
    try:
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        verdict: dict = json.loads(raw_clean)
    except Exception:
        verdict = {"status": "uncertain", "reasoning": "Could not parse LLM response"}

    hyp = {**hyp, "status": verdict.get("status", "uncertain")}
    updated_hyps = [
        h if h["id"] != hyp["id"] else hyp
        for h in state.get("hypotheses", [])
    ]
    return {**state, "active_hypothesis": hyp, "hypotheses": updated_hyps}


def self_consistency(state: dict) -> dict:
    """Run K=3 LLM analyses and pick majority-vote winner (WBS 7.8)."""
    fault_summary = state.get("fault_summary", "")
    hyp = state.get("active_hypothesis")
    evidence = state.get("evidence", [])

    evidence_text = "\n".join(
        f"- [{e['route']}] {e.get('title','')} : {e.get('body','')[:150]}"
        for e in evidence[:6]
    )
    hyp_text = hyp["text"] if hyp else "Unknown fault"

    prompt = (
        f"Fault: {fault_summary}\n"
        f"Leading hypothesis: {hyp_text}\n"
        f"Evidence:\n{evidence_text}\n\n"
        "Provide a concise technical analysis (2-3 sentences) explaining the root cause."
    )

    analyses: list[str] = []
    for _ in range(_K):
        try:
            analyses.append(_llm_call(prompt, temperature=0.3))
        except Exception as exc:
            logger.warning("Self-consistency sample failed: %s", exc)

    if not analyses:
        analyses = [hyp_text]

    # Simple majority vote: pick most common; fallback to first
    from collections import Counter
    # Normalize whitespace for comparison
    normalized = [re.sub(r"\s+", " ", a.strip()) for a in analyses]
    winner = Counter(normalized).most_common(1)[0][0] if normalized else analyses[0]

    return {**state, "candidate_analyses": analyses, "final_analysis": winner}


def bind_claims(state: dict) -> dict:
    """Extract claims from ReAct final answer and verify evidence references (WBS 7.9).

    When the ReAct verdict is insufficient_evidence, skips LLM claim extraction
    and returns an empty claims list — generate_report handles the special branch.
    """
    react_verdict = state.get("react_verdict", "diagnosed")

    # insufficient_evidence / max_iter_reached / budget_exhausted:
    # no verifiable claims can be bound — pass through without LLM call.
    if react_verdict != "diagnosed":
        return {**state, "claims": []}

    analysis = state.get("final_analysis", "") or state.get("react_final_answer", "")
    evidence = state.get("evidence", [])

    evidence_by_hash: dict[str, dict] = {
        e["commit_hash"]: e for e in evidence if e.get("commit_hash")
    }
    evidence_by_bug: dict[int, dict] = {
        e["bug_id"]: e for e in evidence if e.get("bug_id")
    }

    prompt = (
        f"Analysis: {analysis}\n\n"
        "Extract factual claims and cite supporting evidence. "
        "Return JSON array: "
        '[{"text":"claim text","evidence_refs":["commit_hash or bug_id or message_id"]}]'
    )
    raw = _llm_call(prompt)
    try:
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        claims_data: list[dict] = json.loads(raw_clean)
    except Exception:
        claims_data = [{"text": analysis, "evidence_refs": []}]

    claims: list[Claim] = []
    for c in claims_data:
        refs = c.get("evidence_refs", [])
        verified = any(
            r in evidence_by_hash or (r.isdigit() and int(r) in evidence_by_bug)
            for r in refs
        )
        claims.append({
            "text": c.get("text", ""),
            "evidence_refs": refs,
            "verified": verified,
        })

    return {**state, "claims": claims}


def generate_report(state: dict) -> dict:
    """Render the final Markdown and JSON diagnosis report (WBS 7.10)."""
    from agent.report.renderer import render_md, render_json

    report_md = render_md(state)
    report_json = render_json(state)
    return {**state, "report_md": report_md, "report_json": report_json}


# ── Helper ────────────────────────────────────────────────────────────────────


def _llm_call(prompt: str, temperature: float = 0.0) -> str:
    from llm.provider.base import ChatRequest, Message
    from llm.provider.registry import get_provider
    from configs.config import get_config

    cfg = get_config()
    provider = get_provider(cfg.llm.chat.backend)
    req = ChatRequest(
        messages=[Message(role="user", content=prompt)],
        model=cfg.llm.chat.model,
        temperature=temperature,
        stream=False,
    )
    resp = provider.chat(req)
    return resp.content or ""
