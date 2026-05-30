"""LangGraph triage nodes (WBS 7.1; v2 T-001 / T-002 / T-006).

Nodes:
  parse_input                 → detect input type (dmesg / sosreport / question)
  extract_events              → run dmesg extractor or sosreport parser
  detect_taint_and_hw_signals → scan dmesg for taint flags + hardware signals (ADR-023)
  classify_fault_and_route    → pick fault_kind, SOP, and the diagnostic route
  retrieve                    → fire retrieval engine with an LLM-parsed query
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

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

# User framing that implies "something changed" → the `change` route.
# Kept tight to avoid over-routing — only explicit upgrade/regression cues.
_CHANGE_RE = re.compile(
    r"升级|内核更新|更新内核|换了内核|换内核|昨天还|前几天还|之前(?:都)?正常|"
    r"\bupgrad|\bupdat\w*\s+\w*\s*kernel|worked\s+(?:fine\s+)?(?:yesterday|before|"
    r"last\s+\w+)|after\s+(?:the\s+)?(?:update|upgrade)",
    re.IGNORECASE,
)


def parse_input(state: dict) -> dict:
    """Detect whether raw_input is a dmesg blob, file path, or free-form question."""
    raw = state.get("raw_input", "")
    stripped = raw.strip()
    # v2.4: only treat input as a potential path if it actually looks like one
    # — no whitespace / newlines / question marks. Otherwise calling
    # `Path(raw).exists()` on a 250+ char prose blob crashes with ENAMETOOLONG
    # (file-name length limit is 255 on Linux). Run-17 intrinsic-004 hit
    # this when a 294-char free-form question was fed in.
    path_shape = (
        0 < len(stripped) < 260
        and " " not in stripped
        and "\n" not in stripped
        and "?" not in stripped
        and stripped.count("/") <= 12
    )
    p = Path(stripped) if path_shape else None

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


def extract_events(state: dict) -> dict:
    """Extract structured events from dmesg / sosreport input.

    Also exposes the raw dmesg text as `dmesg_text` so the next node
    (detect_taint_and_hw_signals) can scan it for hardware signals.
    """
    input_type = state.get("input_type", "question")
    raw = state.get("raw_input", "")
    events: list[dict[str, Any]] = []
    dmesg_text = ""
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
        dmesg_text = raw
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
                dmesg_text = summary.dmesg_tail
                events = [e.to_dict() for e in _extract(summary.dmesg_tail)]
        except Exception as exc:
            logger.error("sosreport parse failed: %s", exc)

    return {
        **state,
        "kernel_events": events,
        "dmesg_text": dmesg_text,
        "kernel_version": kernel_version,
        "olk_version_tag": olk_version_tag,
        "hostname": hostname,
    }


def detect_taint_and_hw_signals(state: dict) -> dict:
    """Scan dmesg for hardware-error / Machine-Check / taint signals (ADR-023, T-001).

    These gate the `hardware` diagnostic route: a hardlockup or panic with a
    Hardware Error / MCE in dmesg is far more likely a bad-DIMM / firmware
    problem than a kernel bug, and must not be routed to kernel-commit search.
    Detection is deliberately conservative — only strong, explicit signals.
    """
    from mcp_servers.shared.taint_flags import extract_taint_letters

    text = state.get("dmesg_text") or state.get("raw_input", "")
    signals: list[str] = []
    if re.search(r"Hardware Error", text, re.IGNORECASE):
        signals.append("hardware_error")
    if re.search(r"\bMachine Check\b", text, re.IGNORECASE) or \
       re.search(r"^mce:", text, re.IGNORECASE | re.MULTILINE):
        signals.append("mce")
    if re.search(r"EDAC.{0,40}(?:Uncorrected|\bUE\b)", text, re.IGNORECASE):
        signals.append("edac_uncorrected")

    taint = extract_taint_letters(text)
    if "M" in taint:  # M = machine check exception
        signals.append("taint_machine_check")

    # I/O hang signals — surface storage / SAN / fabric trouble distinct from
    # taint or MCE. These DO NOT force the hardware route (P1a v2.1: storage
    # bugs often appear as hung_task / softlockup with kernel-route case
    # framing). Instead they're attached to state so kernel.md prompt can
    # surface "non-kernel root cause" hypotheses (see FAQ Q4 model B fix).
    io_signals: list[str] = []
    if re.search(r"nvme\d+:.*(?:I/O \d+ )?(?:timeout|completion polled|"
                 r"resetting controller|abort|admin command timeout)",
                 text, re.IGNORECASE):
        io_signals.append("nvme_timeout")
    if re.search(r"(?:^|\n)\s*sd \w+:.*(?:timing out|aborting cmd|"
                 r"device offlined)", text, re.IGNORECASE):
        io_signals.append("scsi_timeout")
    if re.search(r"Buffer I/O error on dev", text, re.IGNORECASE):
        io_signals.append("buffer_io_error")
    if re.search(r"blk_update_request: (?:I/O error|critical target|"
                 r"critical medium)", text, re.IGNORECASE):
        io_signals.append("blk_io_error")
    if re.search(r"(?:scsi target|qla\d+|lpfc|bnx2fc).*(?:link offline|"
                 r"link down|Fabric)", text, re.IGNORECASE):
        io_signals.append("san_fabric_event")
    if re.search(r"pcieport.*AER|PCIe Bus Error|"
                 r"Link.*(?:retrain|recovery|degraded)",
                 text, re.IGNORECASE):
        io_signals.append("pcie_link_event")

    return {
        **state,
        "taint_flags": taint,
        "hardware_signals": signals,
        "has_hardware_signal": bool(signals),
        "io_hang_signals": io_signals,
        "has_io_hang_signal": bool(io_signals),
    }


def classify_fault_and_route(state: dict) -> dict:
    """Pick fault_kind, SOP, and the diagnostic route (v2 T-002, replaces classify_fault).

    The route decides which tool subset the Investigation (ReAct) stage exposes.
    Crucially, hardware signals route AWAY from kernel-commit search (ADR-023).
    """
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

    route = _select_route(state, fault_kind, has_events=bool(events))
    sop_name = "hardware" if route == "hardware" \
        else _EVENT_KIND_TO_SOP.get(fault_kind, "generic")
    return {
        **state,
        "fault_kind": fault_kind,
        "fault_summary": fault_summary,
        "sop_name": sop_name,
        "diagnostic_route": route,
    }


def retrieve(state: dict) -> dict:
    """Fire the retrieval engine based on fault summary + kernel version.

    T-006: LLM query parsing is enabled (v1 hard-coded use_llm=False, which
    degraded keyword quality). parse_query falls back to regex on LLM failure,
    so this is safe under rate-limit / budget exhaustion.
    """
    from retrieval.query_parser import parse_query
    from retrieval.engine import retrieve as _retrieve

    question = state.get("fault_summary") or state.get("raw_input", "")
    query = parse_query(question)  # use_llm=True (default); regex fallback on failure
    if state.get("olk_version_tag"):
        query.kernel_version = state["olk_version_tag"]

    result = _retrieve(query)
    return {
        **state,
        "retrieval_query": query.model_dump(),
        "evidence": [e.model_dump() for e in result.items],
    }


# ── Routing helpers (T-002) ───────────────────────────────────────────────────


_CVE_ID_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)


def _select_route(state: dict, fault_kind: str, *, has_events: bool) -> str:
    """ADR-023 routing: hardware signals first, then vmcore, change, kernel.

    Returns one of {hardware, kernel+vmcore, change, kernel, unknown}.

    v2.4 fix: a free-form question that mentions a CVE ID (e.g.
    "Is CVE-2024-26926 fixed in OLK-6.6?") now routes to kernel even
    without dmesg events. Without this, Run-14 cve-001 fell through to
    'unknown' — the toolset is the same but the route framing signals
    "kernel investigation" to the agent.
    """
    if state.get("has_hardware_signal"):
        return "hardware"
    if fault_kind in {"panic", "oops"} and state.get("vmcore_path"):
        return "kernel+vmcore"
    if _implies_recent_change(state):
        return "change"
    if _CVE_ID_RE.search(state.get("raw_input", "") or ""):
        return "kernel"
    if fault_kind == "generic" and not has_events:
        return "unknown"  # vague free-form question — expose the full toolset
    return "kernel"


def _implies_recent_change(state: dict) -> bool:
    """Whether the user's framing suggests 'something changed' (→ change route)."""
    return bool(_CHANGE_RE.search(state.get("raw_input", "")))


# ── LLM fallback classifier ───────────────────────────────────────────────────


def _llm_classify(state: dict) -> tuple[str, str]:
    """Use Navigator LLM to classify fault kind from raw question."""
    from llm.provider.base import ChatRequest, Message
    from llm.provider.registry import get_provider
    from configs.config import get_config
    import json

    cfg = get_config()
    raw = state.get("raw_input", "")
    prompt = (
        "Classify this Linux kernel fault report. Return JSON: "
        '{"fault_kind": "oom|oops|softlockup|hardlockup|panic|lockdep|rcu_stall|generic", '
        '"fault_summary": "<one line>"}.\n\n'
        f"Input: {raw[:1000]}"
    )
    try:
        provider = get_provider(cfg.llm.navigator.backend)
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
