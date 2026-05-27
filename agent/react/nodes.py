"""ReAct Investigation node for agent/graph.py (M22 T-005).

Replaces the v1 load_sop/generate_hypotheses/verify_hypothesis/self_consistency
four-node diagnosis phase with a single ReAct loop node.

State keys consumed:
  diagnostic_route   str       from classify_fault_and_route
  evidence           list      from retrieve
  raw_input          str       original user input
  kernel_version     str | None
  olk_version_tag    str | None
  fault_kind         str | None

State keys produced:
  react_verdict         str   diagnosed | insufficient_evidence | max_iter_reached | budget_exhausted
  react_final_answer    str   the LLM's final answer (non-empty when verdict=diagnosed)
  react_tool_trace      list  [{step, tool, args, error}, ...]
  react_iterations      int
  react_tokens_used     int
  final_analysis        str   alias of react_final_answer (for render_md backward compat)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def react_investigation(state: dict) -> dict:
    """Run the ReAct investigation loop for the current triage route."""
    from llm.provider.registry import get_provider
    from configs.config import get_config
    from agent.react.loop import run_react_loop
    from agent.react.tools import build_registry
    from agent.react.prompts import render_system_prompt

    route = state.get("diagnostic_route") or "unknown"
    cfg = get_config()
    provider = get_provider(cfg.llm.chat.backend)
    registry = build_registry()

    system_prompt = render_system_prompt(route, state)
    user_prompt = _build_user_prompt(state)

    logger.info("ReAct investigation: route=%s", route)
    result = run_react_loop(
        provider=provider,
        registry=registry,
        route=route,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    logger.info(
        "ReAct done: verdict=%s iter=%d tokens=%d",
        result.verdict, result.iterations, result.tokens_used,
    )

    # v2.3: merge ReAct-harvested commit hashes into state.evidence so
    # downstream (bind_claims, recall@10) sees what the agent actually
    # surfaced via search_commits / browse_subsystem_fixes / etc.
    merged_evidence = _merge_react_evidence(
        existing=state.get("evidence", []),
        react_hashes=result.react_evidence_hashes,
    )

    return {
        **state,
        "react_verdict": result.verdict,
        "react_final_answer": result.final_answer,
        "react_tool_trace": result.tool_trace,
        "react_iterations": result.iterations,
        "react_tokens_used": result.tokens_used,
        "evidence": merged_evidence,
        # backward compat: render_md reads final_analysis
        "final_analysis": result.final_answer,
    }


def _merge_react_evidence(existing: list[dict], react_hashes: list[str]) -> list[dict]:
    """Look up subjects for ReAct-harvested hashes and append as evidence rows.

    Existing evidence (from BM25 triage) keeps its order. New rows go to the
    front of the list since the agent surfaced them more recently with more
    context — recall@10 / bind_claims should see them first.

    DB lookup failures are logged but non-fatal; we simply skip the hash.
    """
    if not react_hashes:
        return existing

    seen = {(e.get("commit_hash") or "")[:12] for e in existing if e.get("commit_hash")}
    new_keys = [h for h in react_hashes if h[:12] not in seen]
    if not new_keys:
        return existing

    try:
        from sqlalchemy import text
        from storage.pg.engine import get_engine
        rows: list = []
        with get_engine().connect() as conn:
            # Match by 12-prefix to handle short/long-SHA interchangeably.
            keys12 = list({h[:12] for h in new_keys})
            rs = conn.execute(text("""
                SELECT DISTINCT ON (substring(hash, 1, 12))
                       hash, subject, body
                  FROM kernel_commit
                 WHERE substring(hash, 1, 12) = ANY(:keys)
                 ORDER BY substring(hash, 1, 12), commit_date DESC NULLS LAST
            """), {"keys": keys12}).fetchall()
            rows = list(rs)
    except Exception as exc:
        logger.warning("react evidence DB lookup failed: %s", exc)
        rows = []

    react_rows: list[dict] = []
    for r in rows:
        react_rows.append({
            "route": "commit",
            "score": 0.95,
            "title": (r.subject or "")[:200],
            "body": (r.body or "")[:500],
            "commit_hash": r.hash,
            "metadata": {"source": "react_harvest"},
        })
    return react_rows + existing


def _build_user_prompt(state: dict) -> str:
    """Compose the investigation prompt from triage outputs and retrieved evidence."""
    parts: list[str] = []

    raw = state.get("raw_input", "")
    if raw:
        parts.append(f"## User-provided input\n{raw[:3000]}")

    fault_summary = state.get("fault_summary", "")
    if fault_summary:
        parts.append(f"## Fault summary\n{fault_summary}")

    events = state.get("kernel_events", [])
    if events:
        ev_lines = []
        for e in events[:5]:
            kind = e.get("kind", "unknown")
            summary = e.get("summary", "")
            trace = e.get("call_trace", [])
            ev_lines.append(f"- [{kind}] {summary}")
            for frame in trace[:5]:
                ev_lines.append(f"    {frame}")
        parts.append("## Detected kernel events\n" + "\n".join(ev_lines))

    evidence = state.get("evidence", [])
    if evidence:
        ev_lines = [
            f"- [{e.get('route','')}] score={e.get('score',0):.2f} | {e.get('title','')}"
            f"{' | hash=' + e['commit_hash'][:12] if e.get('commit_hash') else ''}"
            for e in evidence[:10]
        ]
        parts.append("## Pre-retrieved evidence (from BM25 recall)\n" + "\n".join(ev_lines))

    if not parts:
        parts.append("No input provided. Please describe the fault you want to investigate.")

    return "\n\n".join(parts)
