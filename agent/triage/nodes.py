"""LangGraph triage nodes (WBS 7.1).

Nodes:
  parse_input    → detect input type (dmesg / sosreport / question)
  extract_events → run dmesg extractor or sosreport parser
  classify_fault → pick fault_kind and select SOP
  retrieve       → fire retrieval engine with parsed query
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent.triage.state import TriageState

logger = logging.getLogger(__name__)

_EVENT_KIND_TO_SOP = {
    "oom": "oom",
    "softlockup": "lockup",
    "hardlockup": "lockup",
    "rcu_stall": "lockup",
    "panic": "panic",
    "oops": "generic",
    "bug": "generic",
    "warn": "generic",
    "lockdep": "generic",
}


def parse_input(state: TriageState) -> TriageState:
    """Detect whether raw_input is a dmesg blob, file path, or free-form question."""
    raw = state.get("raw_input", "")
    p = Path(raw.strip()) if len(raw) < 500 else None

    if p and p.exists() and p.is_file():
        if "sosreport" in p.name.lower() or p.suffix in (".xz", ".gz", ".bz2"):
            return {**state, "input_type": "sosreport"}
        return {**state, "input_type": "dmesg_file"}

    if p and p.exists() and p.is_dir():
        return {**state, "input_type": "sosreport"}

    # Heuristic: contains kernel log markers → treat as dmesg text
    if any(marker in raw for marker in ("[  ", "BUG:", "WARNING:", "Call Trace:", "Oops:")):
        return {**state, "input_type": "dmesg"}

    return {**state, "input_type": "question"}


def extract_events(state: TriageState) -> TriageState:
    """Extract structured events from dmesg / sosreport input."""
    input_type = state.get("input_type", "question")
    raw = state.get("raw_input", "")
    events: list[dict[str, Any]] = []
    kernel_version = state.get("kernel_version")
    olk_version_tag = state.get("olk_version_tag")
    hostname = state.get("hostname")

    if input_type in ("dmesg", "dmesg_file"):
        from mcp_servers.dmesg_journal.extractor import extract_events as _extract
        if input_type == "dmesg_file":
            try:
                raw = Path(raw.strip()).read_text(errors="replace")
            except Exception as exc:
                logger.error("Failed to read dmesg file: %s", exc)
        events = [e.to_dict() for e in _extract(raw)]

    elif input_type == "sosreport":
        from mcp_servers.sosreport.parser import parse_sosreport
        from mcp_servers.dmesg_journal.extractor import extract_events as _extract
        try:
            summary = parse_sosreport(raw.strip())
            kernel_version = summary.kernel_version or kernel_version
            olk_version_tag = summary.olk_version_tag or olk_version_tag
            hostname = summary.hostname or hostname
            if summary.dmesg_tail:
                events = [e.to_dict() for e in _extract(summary.dmesg_tail)]
        except Exception as exc:
            logger.error("sosreport parse failed: %s", exc)

    return {
        **state,
        "kernel_events": events,
        "kernel_version": kernel_version,
        "olk_version_tag": olk_version_tag,
        "hostname": hostname,
    }


def classify_fault(state: TriageState) -> TriageState:
    """Pick fault_kind and sop_name from the extracted events or LLM fallback."""
    events = state.get("kernel_events", [])

    if events:
        # Priority order: panic > oom > lockup > oops/bug > warn/lockdep
        priority = ["panic", "oom", "softlockup", "hardlockup", "rcu_stall",
                    "oops", "bug", "warn", "lockdep"]
        kinds_seen = {e["kind"] for e in events}
        fault_kind = next((k for k in priority if k in kinds_seen), "generic")
        matching = [e for e in events if e["kind"] == fault_kind]
        fault_summary = matching[0]["summary"] if matching else "Unknown kernel fault"
    else:
        # No dmesg events — use LLM to classify free-form question
        fault_kind, fault_summary = _llm_classify(state)

    sop_name = _EVENT_KIND_TO_SOP.get(fault_kind, "generic")
    return {
        **state,
        "fault_kind": fault_kind,
        "fault_summary": fault_summary,
        "sop_name": sop_name,
    }


def retrieve(state: TriageState) -> TriageState:
    """Fire the retrieval engine based on fault summary + kernel version."""
    from retrieval.query_parser import parse_query
    from retrieval.engine import retrieve as _retrieve

    question = state.get("fault_summary") or state.get("raw_input", "")
    query = parse_query(question, use_llm=False)
    if state.get("olk_version_tag"):
        query.kernel_version = state["olk_version_tag"]

    result = _retrieve(query)
    return {
        **state,
        "retrieval_query": query.model_dump(),
        "evidence": [e.model_dump() for e in result.items],
    }


# ── LLM fallback classifier ───────────────────────────────────────────────────


def _llm_classify(state: TriageState) -> tuple[str, str]:
    """Use Navigator LLM to classify fault kind from raw question."""
    from llm.provider.base import ChatRequest, Message
    from llm.provider.registry import get_provider
    from configs.config import get_config
    import json, re

    cfg = get_config()
    raw = state.get("raw_input", "")
    prompt = (
        "Classify this Linux kernel fault report. Return JSON: "
        '{"fault_kind": "oom|oops|softlockup|hardlockup|panic|lockdep|rcu_stall|generic", '
        '"fault_summary": "<one line>"}.\n\n'
        f"Input: {raw[:1000]}"
    )
    try:
        provider = get_provider(cfg.llm.navigator.provider)
        req = ChatRequest(
            messages=[Message(role="user", content=prompt)],
            model=cfg.llm.navigator.model,
            temperature=0.0,
            stream=False,
        )
        resp = provider.chat(req)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.content or "").strip())
        data = json.loads(content)
        return data.get("fault_kind", "generic"), data.get("fault_summary", raw[:100])
    except Exception:
        return "generic", raw[:100]
