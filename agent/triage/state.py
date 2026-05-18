"""TriageState for the LangGraph diagnosis agent (WBS 7.1).

Triage phase: classify the fault type, determine kernel version, extract
structured problem statement, then hand off to the appropriate SOP.
"""

from __future__ import annotations

from typing import Any, TypedDict

from mcp_servers.dmesg_journal.extractor import EventKind


class TriageState(TypedDict, total=False):
    # ── Input ────────────────────────────────────────────────────────────────
    raw_input: str                  # raw dmesg / sosreport path / free-form question
    input_type: str                 # 'dmesg' | 'sosreport' | 'question'

    # ── Extracted system context ──────────────────────────────────────────────
    kernel_version: str | None      # e.g. "5.10.0-136.12.0.86.olk5.1"
    olk_version_tag: str | None     # 'OLK-6.6' | 'OLK-5.10' | 'mainline'
    hostname: str | None

    # ── Fault classification ──────────────────────────────────────────────────
    fault_kind: str | None          # 'oom' | 'oops' | 'softlockup' | 'hardlockup' |
                                    # 'panic' | 'lockdep' | 'rcu_stall' | 'generic'
    fault_summary: str              # one-line human-readable summary
    kernel_events: list[dict[str, Any]]   # KernelEvent.to_dict() list

    # ── Retrieved evidence (from retrieval engine) ────────────────────────────
    retrieval_query: dict[str, Any] | None
    evidence: list[dict[str, Any]]  # Evidence.to_dict() list

    # ── SOP selection ────────────────────────────────────────────────────────
    sop_name: str | None            # e.g. 'oom' | 'lockup' | 'panic' | 'generic'

    # ── LangGraph control ────────────────────────────────────────────────────
    messages: list[dict[str, Any]]  # accumulated chat messages
    error: str | None               # set on unrecoverable error
