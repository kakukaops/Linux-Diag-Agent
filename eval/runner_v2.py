"""v2 evaluation batch runner (M15 T-022).

Extends the v1 runner with:
  - v2-specific metrics: react_verdict / iterations / tokens_used / tool_calls
  - Route correctness: was the triage route what the case expected?
  - LLM judge for root_cause_correct (uses navigator backend)
  - Hardware / vmcore case support
  - Short SHA prefix matching for recall (fixes 0.0 recall bug in v2 runner)

Usage:
  python -m eval.runner_v2 --dataset eval/data/cases_v2.json --output eval/results_v2/
  python -m eval.runner_v2 --dataset eval/data/cases_v2.json --output eval/results_v2/ --limit 3
  python -m eval.runner_v2 --dataset eval/data/cases_v2.json --output eval/results_v2/ --no-judge
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)


@click.command()
@click.option("--dataset", required=True, type=click.Path(exists=True))
@click.option("--output", required=True, type=click.Path())
@click.option("--limit", default=None, type=int, help="Run only first N cases.")
@click.option("--no-judge", is_flag=True, help="Skip LLM root-cause judge (faster).")
@click.option("--category", default=None, help="Run only cases of this category.")
def main(
    dataset: str,
    output: str,
    limit: int | None,
    no_judge: bool,
    category: str | None,
) -> None:
    """Run v2 batch evaluation on cases_v2.json."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cases = _load_cases(dataset, limit, category)
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        logger.info("Case %d/%d: %s", i, len(cases), case.get("id", "?"))
        result = _run_case(case, use_judge=not no_judge)
        results.append(result)
        (out_dir / f"{case.get('id', i)}.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
        _print_case_result(result)

    summary = _compute_summary(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print_summary(summary)


def _load_cases(
    path: str, limit: int | None, category: str | None
) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data if isinstance(data, list) else data.get("cases", [])
    if category:
        cases = [c for c in cases if c.get("category") == category]
    return cases[:limit] if limit else cases


def _run_case(case: dict[str, Any], *, use_judge: bool = True) -> dict[str, Any]:
    from agent.graph import diagnose

    case_id = case.get("id", "unknown")
    # Use raw_input (dmesg) if available; fall back to question
    raw_input = case.get("raw_input") or case.get("question", "")

    t0 = time.monotonic()
    try:
        state = diagnose(raw_input)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.error("Case %s failed: %s", case_id, exc)
        return {
            "case_id": case_id,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
            **_zero_metrics(),
        }

    # ── Metrics ───────────────────────────────────────────────────────────────
    evidence = state.get("evidence", [])
    claims = state.get("claims", [])

    recall = _compute_recall(evidence, case)
    traceability = (
        sum(1 for c in claims if c.get("verified")) / len(claims)
        if claims else 0.0
    )

    route_correct = (
        state.get("diagnostic_route") == case.get("expected_route")
        if case.get("expected_route") else None
    )
    fault_correct = (
        state.get("fault_kind") == case.get("expected_fault_kind")
        if case.get("expected_fault_kind") else None
    )

    react_verdict = state.get("react_verdict", "diagnosed")
    react_iters = state.get("react_iterations", 0)
    react_tokens = state.get("react_tokens_used", 0)
    tool_trace = state.get("react_tool_trace", [])

    root_cause_correct = None
    judge_reasoning = None
    if use_judge and case.get("ground_truth_root_cause") and react_verdict == "diagnosed":
        root_cause_correct, judge_reasoning = _llm_judge(
            ground_truth=case["ground_truth_root_cause"],
            agent_answer=state.get("final_analysis", "") or state.get("react_final_answer", ""),
        )

    return {
        "case_id": case_id,
        "category": case.get("category"),
        "elapsed_ms": elapsed_ms,
        # Triage correctness
        "fault_kind": state.get("fault_kind"),
        "diagnostic_route": state.get("diagnostic_route"),
        "route_correct": route_correct,
        "fault_kind_correct": fault_correct,
        # ReAct metrics
        "react_verdict": react_verdict,
        "react_iterations": react_iters,
        "react_tokens_used": react_tokens,
        "react_tool_calls": len(tool_trace),
        "react_tools_used": list({t.get("tool") for t in tool_trace}),
        # Evidence quality
        "evidence_count": len(evidence),
        "recall_at_10": round(recall, 3),
        "traceability": round(traceability, 3),
        # Root cause judge
        "root_cause_correct": root_cause_correct,
        "judge_reasoning": judge_reasoning,
        # Report
        "report_md_length": len(state.get("report_md", "")),
        "error": state.get("error"),
    }


def _compute_recall(evidence: list[dict], case: dict) -> float:
    """Recall@10 with short-SHA prefix matching (fixes 0.0 recall from exact-hash mismatch)."""
    expected_commits = set(case.get("expected_commit_hashes", []))
    expected_bugs = set(str(b) for b in case.get("expected_bug_ids", []))

    if not expected_commits and not expected_bugs:
        return 1.0  # no ground truth → unconstrained, treat as pass

    found_commits = set()
    found_bugs = set()
    for ev in evidence[:10]:
        h = ev.get("commit_hash") or ""
        if h:
            found_commits.add(h)
            # Also store 12-char prefix for prefix matching
            found_commits.add(h[:12])
        bid = ev.get("bug_id")
        if bid is not None:
            found_bugs.add(str(bid))

    hit_commits = 0
    for expected in expected_commits:
        short = expected[:12]
        if expected in found_commits or short in found_commits:
            hit_commits += 1

    hit_bugs = sum(1 for b in expected_bugs if b in found_bugs)
    total_expected = len(expected_commits) + len(expected_bugs)
    return (hit_commits + hit_bugs) / total_expected


def _llm_judge(ground_truth: str, agent_answer: str) -> tuple[bool, str]:
    """Use the navigator LLM to judge whether agent_answer matches ground_truth."""
    try:
        from llm.provider.base import ChatRequest, Message
        from llm.provider.registry import get_provider
        from configs.config import get_config
        import re as _re

        cfg = get_config()
        provider = get_provider(cfg.llm.navigator.backend)

        prompt = (
            "You are an expert Linux kernel engineer evaluating a diagnosis report.\n\n"
            f"GROUND TRUTH root cause:\n{ground_truth}\n\n"
            f"AGENT ANSWER:\n{agent_answer[:2000]}\n\n"
            "Does the agent answer correctly identify the root cause? "
            "Answer JSON only: {\"correct\": true|false, \"reasoning\": \"one sentence\"}"
        )
        req = ChatRequest(
            messages=[Message(role="user", content=prompt)],
            model=cfg.llm.navigator.model,
            temperature=0.0,
            stream=False,
        )
        resp = provider.chat(req)
        content = _re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.content or "").strip())
        data = json.loads(content)
        return bool(data.get("correct")), str(data.get("reasoning", ""))
    except Exception as exc:
        logger.warning("LLM judge failed: %s", exc)
        return None, f"judge_error: {exc}"


def _zero_metrics() -> dict:
    return {
        "fault_kind": None,
        "diagnostic_route": None,
        "route_correct": None,
        "fault_kind_correct": None,
        "react_verdict": None,
        "react_iterations": 0,
        "react_tokens_used": 0,
        "react_tool_calls": 0,
        "react_tools_used": [],
        "evidence_count": 0,
        "recall_at_10": 0.0,
        "traceability": 0.0,
        "root_cause_correct": None,
        "judge_reasoning": None,
        "report_md_length": 0,
    }


def _compute_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {"total": 0}

    errors = [r for r in results if r.get("error")]
    ok = [r for r in results if not r.get("error")]

    def avg(key: str) -> float:
        vals = [r[key] for r in ok if r.get(key) is not None]
        return round(sum(vals) / max(len(vals), 1), 3)

    def frac(key: str) -> float:
        vals = [r[key] for r in ok if r.get(key) is not None]
        return round(sum(1 for v in vals if v) / max(len(vals), 1), 3)

    # Verdicts breakdown
    verdicts: dict[str, int] = {}
    for r in ok:
        v = r.get("react_verdict") or "unknown"
        verdicts[v] = verdicts.get(v, 0) + 1

    # Route accuracy
    route_results = [r for r in ok if r.get("route_correct") is not None]
    route_acc = (
        sum(1 for r in route_results if r["route_correct"]) / len(route_results)
        if route_results else None
    )

    # Fault kind accuracy
    fault_results = [r for r in ok if r.get("fault_kind_correct") is not None]
    fault_acc = (
        sum(1 for r in fault_results if r["fault_kind_correct"]) / len(fault_results)
        if fault_results else None
    )

    # Judge results
    judge_results = [r for r in ok if r.get("root_cause_correct") is not None]
    judge_acc = (
        sum(1 for r in judge_results if r["root_cause_correct"]) / len(judge_results)
        if judge_results else None
    )

    return {
        "total": n,
        "errors": len(errors),
        "avg_recall_at_10": avg("recall_at_10"),
        "avg_traceability": avg("traceability"),
        "avg_elapsed_ms": avg("elapsed_ms"),
        "avg_react_iterations": avg("react_iterations"),
        "avg_react_tokens": avg("react_tokens_used"),
        "avg_tool_calls": avg("react_tool_calls"),
        "verdicts": verdicts,
        "route_accuracy": round(route_acc, 3) if route_acc is not None else None,
        "fault_kind_accuracy": round(fault_acc, 3) if fault_acc is not None else None,
        "root_cause_accuracy": round(judge_acc, 3) if judge_acc is not None else None,
        "per_category": _per_category(ok),
    }


def _per_category(results: list[dict]) -> dict[str, Any]:
    cats: dict[str, list] = {}
    for r in results:
        c = r.get("category", "unknown")
        cats.setdefault(c, []).append(r)
    out = {}
    for cat, items in cats.items():
        out[cat] = {
            "count": len(items),
            "avg_recall": round(
                sum(r.get("recall_at_10", 0) for r in items) / len(items), 3
            ),
            "route_correct_pct": round(
                sum(1 for r in items if r.get("route_correct")) / len(items), 3
            ),
        }
    return out


def _print_case_result(r: dict) -> None:
    status = r.get("react_verdict") or ("ERROR" if r.get("error") else "?")
    route = r.get("diagnostic_route") or "?"
    route_ok = "✓" if r.get("route_correct") else ("?" if r.get("route_correct") is None else "✗")
    recall = r.get("recall_at_10", 0)
    judge = r.get("root_cause_correct")
    judge_str = ("✓" if judge else "✗") if judge is not None else "-"
    print(
        f"  [{r['case_id']:<20}] {status:<22} route={route:<14}{route_ok} "
        f"recall={recall:.0%} judge={judge_str} "
        f"iter={r.get('react_iterations',0)} tok={r.get('react_tokens_used',0):,}"
    )


def _print_summary(s: dict) -> None:
    print(f"\n{'='*65}")
    print(f"v2 Eval Summary  ({s['total']} cases, {s.get('errors',0)} errors)")
    print(f"  Recall@10:          {s.get('avg_recall_at_10', 0):.1%}")
    print(f"  Traceability:       {s.get('avg_traceability', 0):.1%}")
    print(f"  Route accuracy:     {s['route_accuracy']:.1%}" if s.get('route_accuracy') is not None else "  Route accuracy:     n/a")
    print(f"  Fault-kind acc:     {s['fault_kind_accuracy']:.1%}" if s.get('fault_kind_accuracy') is not None else "  Fault-kind acc:     n/a")
    print(f"  Root-cause acc:     {s['root_cause_accuracy']:.1%}" if s.get('root_cause_accuracy') is not None else "  Root-cause acc:     n/a")
    print(f"  Avg latency:        {s.get('avg_elapsed_ms', 0)/1000:.1f}s")
    print(f"  Avg ReAct iters:    {s.get('avg_react_iterations', 0):.1f}")
    print(f"  Avg tokens used:    {s.get('avg_react_tokens', 0):,.0f}")
    verdicts = s.get("verdicts", {})
    if verdicts:
        print(f"  Verdicts:           {verdicts}")
    cats = s.get("per_category", {})
    if cats:
        print(f"  Per category:")
        for cat, data in cats.items():
            print(f"    {cat:<16} n={data['count']} recall={data['avg_recall']:.0%} route={data['route_correct_pct']:.0%}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
