"""DiagnosisState for the LangGraph diagnosis agent (WBS 7.2).

Diagnosis phase: generate hypotheses, gather evidence, verify, produce report.
"""

from __future__ import annotations

from typing import Any, TypedDict


class Hypothesis(TypedDict):
    id: str
    text: str                    # "Memory overcommit caused OOM due to..."
    confidence: float            # 0-1
    supporting_evidence: list[str]   # Evidence titles/IDs
    status: str                  # 'pending' | 'confirmed' | 'rejected'


class Claim(TypedDict):
    text: str
    evidence_refs: list[str]     # commit hashes, bug IDs, message_ids
    verified: bool


class DiagnosisState(TypedDict, total=False):
    # ── Inherited from triage ─────────────────────────────────────────────────
    fault_kind: str
    fault_summary: str
    olk_version_tag: str | None
    kernel_events: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    sop_name: str | None

    # ── SOP execution ─────────────────────────────────────────────────────────
    sop_steps: list[str]         # loaded from SOP yaml
    current_step: int

    # ── Hypothesis tracking ───────────────────────────────────────────────────
    hypotheses: list[Hypothesis]
    active_hypothesis: Hypothesis | None

    # ── Claim-Evidence binding ────────────────────────────────────────────────
    claims: list[Claim]

    # ── Self-consistency ─────────────────────────────────────────────────────
    candidate_analyses: list[str]    # K=3 LLM outputs for voting
    final_analysis: str              # majority-vote winner

    # ── Report ────────────────────────────────────────────────────────────────
    report_md: str               # Markdown diagnosis report
    report_json: dict[str, Any]  # JSON schema output

    # ── LangGraph control ────────────────────────────────────────────────────
    messages: list[dict[str, Any]]
    iteration: int               # loop counter
    error: str | None
