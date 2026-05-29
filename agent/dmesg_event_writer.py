"""Persist a dmesg_event row at the end of each diagnosis session.

Closes the v2.3 "I've seen this before" feedback loop. Previously
`find_similar_crashes` could query syzbot_crash / bug / lkml_message
but never `dmesg_event`, because no node ever wrote to it. After this
hook every completed agent run leaves a row keyed on its
stack_signature, so future similar inputs can short-circuit straight
to the past diagnosis (and the agent stops re-deriving the same
conclusion from scratch).

Idempotent: ON CONFLICT (event_id) DO NOTHING. The event_id is a
content hash of (raw_input + fault_kind), so identical inputs collapse
to one row.
"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def write_dmesg_event(state: dict) -> dict:
    """LangGraph node: persist the final state as a dmesg_event row.

    Best-effort. Failures log + skip — they must NOT break the
    diagnosis pipeline since the report has already been generated.
    """
    try:
        _write(state)
    except Exception as exc:
        logger.warning("dmesg_event write failed (non-fatal): %s", exc)
    return state


def _write(state: dict) -> None:
    from sqlalchemy import text
    from storage.pg.engine import get_engine
    from kg.signature import extract_trace_from_text, stack_signature

    raw_input = state.get("raw_input") or ""
    if not raw_input.strip():
        return

    fault_kind = state.get("fault_kind") or "unknown"
    kernel_version = state.get("kernel_version")
    olk_version = state.get("olk_version_tag")
    input_type = state.get("input_type") or "user-input"

    # event_id: content hash so re-runs of the same input collapse
    h = hashlib.sha256()
    h.update(raw_input.encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(fault_kind.encode("utf-8"))
    event_id = h.hexdigest()[:32]

    # Extract trace + compute signature (kg.signature module)
    trace = extract_trace_from_text(raw_input) or ""
    sig = stack_signature(trace) if trace else ""

    # Compact metadata: enough to reconstruct the diagnosis context
    # but not the full body (those are in report_md if needed).
    metadata = {
        "react_verdict": state.get("react_verdict"),
        "react_iterations": state.get("react_iterations"),
        "react_tokens_used": state.get("react_tokens_used"),
        "groundedness": state.get("groundedness"),
        "n_kernel_events": len(state.get("kernel_events") or []),
        "tools_used": list({t.get("tool")
                             for t in (state.get("react_tool_trace") or [])
                             if isinstance(t, dict)}),
    }

    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO dmesg_event
                (event_id, source, fault_kind, kernel_version, olk_version_tag,
                 raw_text, call_trace, stack_signature, metadata)
            VALUES
                (:eid, :src, :fk, :kv, :olk, :raw, :trace, :sig,
                 CAST(:meta AS jsonb))
            ON CONFLICT (event_id) DO NOTHING
        """), {
            "eid": event_id,
            "src": input_type,
            "fk": fault_kind,
            "kv": kernel_version,
            "olk": olk_version,
            "raw": raw_input[:50_000],   # cap so we don't blow up the row
            "trace": trace[:20_000] if trace else None,
            "sig": sig or None,
            "meta": json.dumps(metadata),
        })
    logger.info("dmesg_event persisted: event_id=%s sig=%s fault=%s",
                event_id[:12], (sig or "?")[:12], fault_kind)
