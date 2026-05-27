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


# Stopwords filtered out before keyword overlap (kernel-context tuned).
_STOPWORDS = frozenset({
    "the","a","an","is","are","was","were","be","being","been","of","to","in",
    "on","at","by","for","with","this","that","it","its","as","and","or","but",
    "not","no","if","then","else","when","which","who","what","where","how",
    "from","into","because","due","cause","root","kernel","linux","commit",
    "patch","fix","fixes","bug","issue","problem","occurs","occur","may","can",
    "should","would","could","one","two","some","any","all","may","might",
    "thus","therefore","hence","also","both","each","more","most","less",
})


def _significant_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens ≥ 4 chars, stopwords removed."""
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", (text or "").lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _claim_evidence_overlap(claim_text: str, ev: dict) -> float:
    """Fraction of significant claim tokens present in evidence body+title.

    Returns 0.0 if claim has no significant tokens (we treat empty as
    no-overlap, NOT trivially verified).
    """
    claim_toks = _significant_tokens(claim_text)
    if not claim_toks:
        return 0.0
    ev_text = (ev.get("title") or "") + " " + (ev.get("body") or "")
    ev_toks = _significant_tokens(ev_text)
    if not ev_toks:
        return 0.0
    overlap = claim_toks & ev_toks
    return len(overlap) / len(claim_toks)


_OVERLAP_THRESHOLD = 0.20  # ≥ 20% of claim's significant tokens must appear
                            # in the cited evidence to count as "grounded"


def bind_claims(state: dict) -> dict:
    """Extract claims from ReAct final answer and verify evidence references (WBS 7.9).

    v2.1 hardened (P0-1 + P0-2):
      - Each claim is checked twofold:
        (a) does evidence_refs contain any hash/bug_id present in our retrieved
            evidence?  (existence — original v2.0 check)
        (b) does the claim TEXT have ≥20% significant-token overlap with that
            evidence's title/body?  (semantic — guards against LLM citing
            unrelated commits)
      - verified = (a) AND (b)
      - Each claim gets evidence_strength ∈ [0, 1] (max overlap across cited
        refs that exist).
      - Computes state['groundedness']:
          "grounded"     — ≥50 % of claims verified, all evidence_strength ≥ 0.2
          "speculative"  — diagnosed but most claims fail one of the checks
          "n/a"          — react_verdict ≠ diagnosed (no claims to bind)
      - Claims are split into verified_claims and speculative_claims for the
        renderer; the previous "all-claims-with-⚠" approach is gone.

    Insufficient verdicts (insufficient_evidence / max_iter_reached /
    budget_exhausted) skip claim extraction entirely as before.
    """
    react_verdict = state.get("react_verdict", "diagnosed")

    if react_verdict != "diagnosed":
        return {**state, "claims": [], "verified_claims": [],
                "speculative_claims": [], "groundedness": "n/a"}

    analysis = state.get("final_analysis", "") or state.get("react_final_answer", "")
    evidence = state.get("evidence", [])

    evidence_by_hash: dict[str, dict] = {
        e["commit_hash"]: e for e in evidence if e.get("commit_hash")
    }
    evidence_by_bug: dict[int, dict] = {
        e["bug_id"]: e for e in evidence if e.get("bug_id")
    }
    evidence_by_msg: dict[str, dict] = {
        e["message_id"]: e for e in evidence if e.get("message_id")
    }

    # v2.2 Path C: show the LLM what evidence is ACTUALLY in the retrieved
    # pool, so it can cite real IDs (not invent from training memory).
    # Truncate per-row text to keep prompt tractable.
    evidence_lines: list[str] = []
    for i, ev in enumerate(evidence[:30]):  # top 30 evidence rows
        rid = (ev.get("commit_hash") or
               (f"bug#{ev['bug_id']}" if ev.get("bug_id") else None) or
               ev.get("message_id") or
               ev.get("cve_id") or
               f"item{i}")
        title = (ev.get("title") or "")[:80]
        evidence_lines.append(f"  - {rid}: {title}")
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "  (no evidence retrieved)"

    prompt = (
        f"Analysis: {analysis}\n\n"
        f"Retrieved evidence pool (cite ONLY from this list):\n{evidence_block}\n\n"
        "Extract factual claims from the Analysis and cite the SINGLE most-supporting "
        "evidence ID for each. Each claim's evidence_refs must contain IDs that appear "
        "above. If a claim has no matching evidence in the pool, set evidence_refs to []. "
        "Do NOT invent commit hashes or bug IDs.\n\n"
        "Return JSON array only: "
        '[{"text":"claim text","evidence_refs":["id-from-pool"]}]'
    )
    raw = _llm_call(prompt)
    try:
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        claims_data: list[dict] = json.loads(raw_clean)
    except Exception:
        claims_data = [{"text": analysis, "evidence_refs": []}]

    claims: list[Claim] = []
    verified_claims: list[Claim] = []
    speculative_claims: list[Claim] = []
    for c in claims_data:
        text = c.get("text", "") or ""
        refs = c.get("evidence_refs") or []

        # (a) which refs resolve to actual evidence rows?
        cited_evidence: list[dict] = []
        for r in refs:
            if r in evidence_by_hash:
                cited_evidence.append(evidence_by_hash[r])
            elif isinstance(r, str) and r.isdigit() and int(r) in evidence_by_bug:
                cited_evidence.append(evidence_by_bug[int(r)])
            elif r in evidence_by_msg:
                cited_evidence.append(evidence_by_msg[r])

        # (b) max semantic overlap across cited evidence
        if cited_evidence:
            evidence_strength = max(_claim_evidence_overlap(text, ev)
                                    for ev in cited_evidence)
        else:
            evidence_strength = 0.0

        # (c) v2.2 Path C — auto-attach rescue.
        # If LLM-provided refs don't pass the overlap check, scan the FULL
        # retrieved evidence pool for a strong-overlap match and attach it.
        # Handles two LLM failure modes:
        #   (i)  cited unrelated commits (claim is right but wrong ref)
        #   (ii) forgot to cite any evidence at all
        # The auto-attached ref must clear the SAME 0.20 threshold — we're
        # not lowering the bar, just letting the agent find the right
        # supporting evidence after the fact.
        if evidence_strength < _OVERLAP_THRESHOLD:
            best_ev = None
            best_overlap = 0.0
            for ev in evidence:
                ov = _claim_evidence_overlap(text, ev)
                if ov > best_overlap:
                    best_overlap = ov
                    best_ev = ev
            if best_ev and best_overlap >= _OVERLAP_THRESHOLD:
                # Take whichever ID this evidence carries
                auto_ref = (best_ev.get("commit_hash")
                            or (str(best_ev["bug_id"]) if best_ev.get("bug_id") else None)
                            or best_ev.get("message_id")
                            or best_ev.get("cve_id"))
                if auto_ref:
                    cited_evidence.append(best_ev)
                    evidence_strength = best_overlap
                    refs = list(refs) + [f"AUTO:{auto_ref}"]

        verified = bool(cited_evidence) and evidence_strength >= _OVERLAP_THRESHOLD
        claim: Claim = {
            "text": text,
            "evidence_refs": refs,
            "verified": verified,
            "evidence_strength": round(evidence_strength, 2),
        }
        claims.append(claim)
        (verified_claims if verified else speculative_claims).append(claim)

    # Groundedness aggregate (used by renderer + eval metrics)
    if not claims:
        groundedness = "speculative"  # diagnosed but extracted 0 claims = thin
    elif len(verified_claims) / len(claims) >= 0.5:
        groundedness = "grounded"
    else:
        groundedness = "speculative"

    return {
        **state,
        "claims": claims,
        "verified_claims": verified_claims,
        "speculative_claims": speculative_claims,
        "groundedness": groundedness,
    }


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
