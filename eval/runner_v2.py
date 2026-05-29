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
@click.option("--case-ids", default=None,
              help="Comma-separated list of case ids to run (overrides --limit/--category).")
@click.option("--merge-summary", is_flag=True,
              help="After running, merge with existing per-case JSONs in output dir to "
                   "recompute summary.json. Useful for partial reruns.")
@click.option("--concurrency", default=1, type=int,
              help="Run N cases in parallel (default 1; OpenRouter 600 rpm + lore 40 rpm "
                   "shared limiters handle 3-4 safely).")
def main(
    dataset: str,
    output: str,
    limit: int | None,
    no_judge: bool,
    category: str | None,
    case_ids: str | None,
    merge_summary: bool,
    concurrency: int,
) -> None:
    """Run v2 batch evaluation on cases_v2.json."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cases = _load_cases(dataset, limit, category, case_ids)
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    results: list[dict[str, Any]] = []
    if concurrency <= 1:
        for i, case in enumerate(cases, 1):
            logger.info("Case %d/%d: %s", i, len(cases), case.get("id", "?"))
            result = _run_case(case, use_judge=not no_judge)
            _save_case(result, case, out_dir)
            _print_case_result(result)
            results.append(result)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        logger.info("Running %d cases with concurrency=%d", len(cases), concurrency)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_run_case, c, use_judge=not no_judge): c for c in cases}
            done = 0
            for fut in as_completed(futures):
                case = futures[fut]
                done += 1
                try:
                    result = fut.result()
                except Exception as exc:
                    logger.exception("Case %s crashed: %s", case.get("id"), exc)
                    result = _make_error_result(case, str(exc))
                _save_case(result, case, out_dir)
                logger.info("Case %d/%d done: %s", done, len(cases), case.get("id"))
                _print_case_result(result)
                results.append(result)
    elapsed = time.monotonic() - t0
    logger.info("Total eval wall time: %.1fs (%.1f min)", elapsed, elapsed / 60)

    if merge_summary:
        # Recompute summary over ALL per-case JSONs in out_dir, not just this run
        all_results: list[dict[str, Any]] = []
        for f in sorted(out_dir.glob("*.json")):
            if f.name == "summary.json":
                continue
            try:
                all_results.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("skip %s: %s", f, exc)
        logger.info("merge_summary: combined %d existing case JSONs", len(all_results))
        summary = _compute_summary(all_results)
    else:
        summary = _compute_summary(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print_summary(summary)


def _save_case(result: dict, case: dict, out_dir: Path) -> None:
    (out_dir / f"{case.get('id', 'unknown')}.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )


def _load_cases(
    path: str, limit: int | None, category: str | None, case_ids: str | None = None,
) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data if isinstance(data, list) else data.get("cases", [])
    if case_ids:
        wanted = {s.strip() for s in case_ids.split(",") if s.strip()}
        cases = [c for c in cases if c.get("id") in wanted]
        return cases
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

    # v2.1 P1-1: groundedness from bind_claims (None if not yet present)
    groundedness = state.get("groundedness")
    n_verified = len(state.get("verified_claims") or [])
    n_speculative = len(state.get("speculative_claims") or [])
    n_total_claims = n_verified + n_speculative

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
        # v2.4 Goal-1 KG-path coverage. Per the 3-goal design lens:
        # Goal 1 (KG efficiency) cases declare `expected_kg_paths` — the
        # set of KG tools we predict an effective agent would use. This
        # metric measures whether the agent actually went down those paths,
        # independent of whether it found the GT commit. High coverage +
        # low recall = "agent looked in the right places but the answer
        # wasn't there"; low coverage + low recall = "agent didn't even try
        # the available paths".
        "expected_kg_paths": case.get("expected_kg_paths") or [],
        "expected_kg_paths_coverage": _coverage(
            case.get("expected_kg_paths") or [],
            {t.get("tool") for t in tool_trace if not t.get("error")},
        ),
        "primary_goal": case.get("primary_goal") or "unclassified",
        # v2.3 P2: persist the full tool_trace including args so post-hoc
        # analysis can answer "what symbol did the LLM actually query
        # vs what it should have queried". Without args we can only count
        # tool names — insufficient to diagnose recall failures.
        "react_tool_trace": [
            {
                "step": t.get("step"),
                "tool": t.get("tool"),
                "args": (t.get("args") or "")[:500],  # truncate huge JSONs
                "error": t.get("error", False),
            }
            for t in tool_trace
        ],
        # Evidence quality
        "evidence_count": len(evidence),
        "recall_at_10": round(recall, 3) if recall is not None else None,
        "traceability": round(traceability, 3),
        # v2.1: claim grounding
        "groundedness": groundedness,
        "n_verified_claims": n_verified,
        "n_speculative_claims": n_speculative,
        "verified_claim_rate": (
            round(n_verified / n_total_claims, 3) if n_total_claims else None
        ),
        # Root cause judge
        "root_cause_correct": root_cause_correct,
        "judge_reasoning": judge_reasoning,
        # Report
        "report_md_length": len(state.get("report_md", "")),
        # v2.4 audit: persist the actual final analysis so post-hoc reading
        # can answer "did the agent really get it wrong, or did the judge
        # disagree with a defensible alternative?". Capped to keep file size
        # reasonable on long answers.
        "react_final_answer": (state.get("react_final_answer") or "")[:4000],
        "final_analysis": (state.get("final_analysis") or "")[:4000],
        "error": state.get("error"),
    }


def _coverage(expected: list[str], actual_set: set[str]) -> float | None:
    """KG-path coverage = |expected ∩ actual| / |expected|.

    Returns None when expected is empty — for Goal-3 cases (intrinsic
    knowledge / KG silent by design) there's no expected path set,
    so coverage is not applicable. The runner / summary skip None.
    """
    if not expected:
        return None
    hits = sum(1 for p in expected if p in actual_set)
    return round(hits / len(expected), 3)


def _compute_recall(evidence: list[dict], case: dict) -> float | None:
    """Recall@10 with short-SHA prefix matching (fixes 0.0 recall from exact-hash mismatch).

    Returns None when the case has no ground-truth commits/bugs — either
    because the case is `no_commit_expected: True` by design (hardware /
    config / regression cases) or because ground truth has not yet been
    researched. Both are excluded from the recall aggregate so we don't
    inflate the headline (which is what the old `return 1.0` did).
    """
    if case.get("no_commit_expected"):
        return None    # by design: not a commit-citable case

    expected_commits = set(case.get("expected_commit_hashes", []))
    expected_bugs = set(str(b) for b in case.get("expected_bug_ids", []))

    if not expected_commits and not expected_bugs:
        return None    # no ground truth yet (needs research)

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


_JUDGE_PROMPT_AFFIRMATIVE = """\
You are an expert Linux kernel engineer evaluating a diagnosis report.

GROUND TRUTH root cause:
{gt}

AGENT ANSWER:
{ans}

Does the agent answer correctly identify the root cause?
Answer JSON only: {{"correct": true|false, "reasoning": "one sentence"}}
"""

_JUDGE_PROMPT_DEVILS_ADVOCATE = """\
You are an expert Linux kernel engineer auditing a diagnosis report for
INACCURACIES. Your job is to find at least one specific claim in the agent
answer that contradicts the ground truth, or to confirm everything checks out.

GROUND TRUTH root cause:
{gt}

AGENT ANSWER:
{ans}

If you can identify ANY specific factual mismatch (wrong subsystem, wrong
mechanism, wrong commit cited, missing key element), the answer is INCORRECT.
Only if every substantive claim aligns with the ground truth is the answer
CORRECT.

Answer JSON only: {{"correct": true|false, "reasoning": "one sentence describing the mismatch or confirming alignment"}}
"""


def _llm_judge_single(prompt: str) -> tuple[bool | None, str]:
    """Run one LLM judge query, return (correct?, reasoning) or (None, err)."""
    try:
        from llm.provider.base import ChatRequest, Message
        from llm.provider.registry import get_provider
        from configs.config import get_config
        import re as _re

        cfg = get_config()
        provider = get_provider(cfg.llm.navigator.backend)
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
        return None, f"judge_error: {exc}"


def _llm_judge(ground_truth: str, agent_answer: str) -> tuple[bool | None, str]:
    """v2.1 P1-2: dual-judge with prompt-framing variance.

    Two LLM passes:
      A. Affirmative framing — "does it correctly identify the root cause?"
         Default lean is yes-leaning (LLM tends to validate plausible answers).
      B. Devil's-advocate framing — "find ANY mismatch; only confirm if all
         claims align." Default lean is no-leaning.

    Aggregate:
      A=True  & B=True  → True   (both lenses confirm correct)
      A=False & B=False → False  (both lenses confirm wrong)
      A!=B              → None   (judge_uncertain — excluded from accuracy)

    This catches the most common judge failures: lenient yes-bias on
    plausible-but-wrong answers, and harsh no-bias on technically-correct
    but differently-worded answers. Genuinely different models would be
    better; same-model-different-framing is the cheap proxy.
    """
    ans = agent_answer[:2000]
    a_correct, a_reason = _llm_judge_single(
        _JUDGE_PROMPT_AFFIRMATIVE.format(gt=ground_truth, ans=ans)
    )
    b_correct, b_reason = _llm_judge_single(
        _JUDGE_PROMPT_DEVILS_ADVOCATE.format(gt=ground_truth, ans=ans)
    )

    # Both judges failed → error
    if a_correct is None and b_correct is None:
        logger.warning("Both judges failed: A=%s B=%s", a_reason, b_reason)
        return None, f"judge_a_failed: {a_reason} | judge_b_failed: {b_reason}"
    # One failed → fall back to the working one
    if a_correct is None:
        return b_correct, f"[only judge B available] {b_reason}"
    if b_correct is None:
        return a_correct, f"[only judge A available] {a_reason}"
    # Both succeeded — check agreement
    if a_correct == b_correct:
        return a_correct, f"[both agree] A: {a_reason} | B: {b_reason}"
    # Disagreement → judge_uncertain (excluded from accuracy denominator)
    return None, f"[judges disagree] A({a_correct}): {a_reason} | B({b_correct}): {b_reason}"


def _make_error_result(case: dict, err: str) -> dict:
    return {
        "case_id": case.get("id", "unknown"),
        "category": case.get("category"),
        "error": err,
        "elapsed_ms": 0.0,
        **_zero_metrics(),
    }


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
        "groundedness": None,
        "n_verified_claims": 0,
        "n_speculative_claims": 0,
        "verified_claim_rate": None,
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

    # Judge results — dual-judge (P1-2) returns None for "judges disagreed"
    diagnosed_with_gt = [r for r in ok if r.get("react_verdict") == "diagnosed"
                         and r.get("judge_reasoning")]  # judged at all
    judge_results = [r for r in diagnosed_with_gt if r.get("root_cause_correct") is not None]
    judge_uncertain = [r for r in diagnosed_with_gt if r.get("root_cause_correct") is None]
    judge_acc = (
        sum(1 for r in judge_results if r["root_cause_correct"]) / len(judge_results)
        if judge_results else None
    )
    judge_uncertain_rate = (
        round(len(judge_uncertain) / len(diagnosed_with_gt), 3)
        if diagnosed_with_gt else None
    )

    # v2.1 P1-1 — grounding-aware metrics (anti-overconfidence; the user
    # wants accuracy not score-gaming):
    #
    #   grounded_rate    = of cases reaching diagnosed, what % had
    #                      groundedness=="grounded" (claims trace to evidence)
    #   speculation_rate = of cases reaching diagnosed, what % were speculative
    #   abstention_rate  = of ALL cases, what % chose insufficient_evidence
    #                      (a CORRECT outcome when evidence is missing — not
    #                      counted as failure)
    #   grounded_correct = of grounded cases that were ALSO judge-correct
    #                      (the strictest, most-honest accuracy measure)
    #
    diagnosed = [r for r in ok if r.get("react_verdict") == "diagnosed"]
    grounded = [r for r in diagnosed if r.get("groundedness") == "grounded"]
    speculative = [r for r in diagnosed if r.get("groundedness") == "speculative"]
    abstained = [r for r in ok if r.get("react_verdict") == "insufficient_evidence"]

    grounded_rate = (
        round(len(grounded) / len(diagnosed), 3) if diagnosed else None
    )
    speculation_rate = (
        round(len(speculative) / len(diagnosed), 3) if diagnosed else None
    )
    abstention_rate = round(len(abstained) / len(ok), 3) if ok else None

    # Strictest accuracy: judge-correct AND grounded
    grounded_judged = [r for r in grounded if r.get("root_cause_correct") is not None]
    grounded_correct_rate = (
        round(sum(1 for r in grounded_judged if r["root_cause_correct"]) /
              len(grounded_judged), 3)
        if grounded_judged else None
    )

    # avg claim-verification rate across diagnosed cases that had claims
    diag_with_claims = [r for r in diagnosed if r.get("verified_claim_rate") is not None]
    avg_verified_claim_rate = (
        round(sum(r["verified_claim_rate"] for r in diag_with_claims) /
              len(diag_with_claims), 3)
        if diag_with_claims else None
    )

    recall_scored = [r for r in ok if r.get("recall_at_10") is not None]

    # v2.4 Goal-1 KG-path coverage (across cases that declared expected_kg_paths)
    coverage_scored = [r for r in ok
                       if r.get("expected_kg_paths_coverage") is not None]
    avg_kg_coverage = (
        round(sum(r["expected_kg_paths_coverage"] for r in coverage_scored) /
              len(coverage_scored), 3)
        if coverage_scored else None
    )

    # v2.4 per-primary-goal breakout: Goal-1 (KG efficiency) and
    # Goal-3 (intrinsic knowledge) are evaluated under DIFFERENT lenses
    # — mixing them washed out the signal in prior runs.
    by_goal: dict[str, list] = {}
    for r in ok:
        by_goal.setdefault(r.get("primary_goal", "unclassified"), []).append(r)
    goal_breakdown = {}
    for goal, rs in by_goal.items():
        diag = [x for x in rs if x.get("react_verdict") == "diagnosed"]
        grd = [x for x in diag if x.get("groundedness") == "grounded"]
        grd_judged = [x for x in grd if x.get("root_cause_correct") is not None]
        cov_rs = [x for x in rs if x.get("expected_kg_paths_coverage") is not None]
        rec_rs = [x for x in rs if x.get("recall_at_10") is not None]
        goal_breakdown[goal] = {
            "n": len(rs),
            "diagnosed": len(diag),
            "grounded": len(grd),
            "grounded_correct_rate": (
                round(sum(1 for x in grd_judged if x["root_cause_correct"]) /
                      len(grd_judged), 3) if grd_judged else None),
            "avg_recall_at_10": (
                round(sum(x["recall_at_10"] for x in rec_rs) / len(rec_rs), 3)
                if rec_rs else None),
            "avg_kg_path_coverage": (
                round(sum(x["expected_kg_paths_coverage"] for x in cov_rs) /
                      len(cov_rs), 3) if cov_rs else None),
        }

    return {
        "total": n,
        "errors": len(errors),
        "avg_recall_at_10": avg("recall_at_10"),
        "recall_scored_n": len(recall_scored),
        "avg_kg_path_coverage": avg_kg_coverage,
        "kg_coverage_scored_n": len(coverage_scored),
        "per_goal": goal_breakdown,
        "avg_traceability": avg("traceability"),
        "avg_elapsed_ms": avg("elapsed_ms"),
        "avg_react_iterations": avg("react_iterations"),
        "avg_react_tokens": avg("react_tokens_used"),
        "avg_tool_calls": avg("react_tool_calls"),
        "verdicts": verdicts,
        "route_accuracy": round(route_acc, 3) if route_acc is not None else None,
        "fault_kind_accuracy": round(fault_acc, 3) if fault_acc is not None else None,
        "root_cause_accuracy": round(judge_acc, 3) if judge_acc is not None else None,
        # v2.1 grounding metrics
        "grounded_rate": grounded_rate,
        "speculation_rate": speculation_rate,
        "abstention_rate": abstention_rate,
        "grounded_correct_rate": grounded_correct_rate,
        "avg_verified_claim_rate": avg_verified_claim_rate,
        "judge_uncertain_rate": judge_uncertain_rate,
        "per_category": _per_category(ok),
    }


def _per_category(results: list[dict]) -> dict[str, Any]:
    cats: dict[str, list] = {}
    for r in results:
        c = r.get("category", "unknown")
        cats.setdefault(c, []).append(r)
    out = {}
    for cat, items in cats.items():
        scored = [r for r in items if r.get("recall_at_10") is not None]
        out[cat] = {
            "count": len(items),
            "recall_scored_n": len(scored),
            "avg_recall": (
                round(sum(r["recall_at_10"] for r in scored) / len(scored), 3)
                if scored else None
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
    recall_raw = r.get("recall_at_10")
    recall_str = f"{recall_raw:.0%}" if recall_raw is not None else "n/a"
    judge = r.get("root_cause_correct")
    judge_str = ("✓" if judge else "✗") if judge is not None else "-"
    gnd = r.get("groundedness") or "-"
    gnd_str = {"grounded": "G", "speculative": "S", "n/a": "-"}.get(gnd, "?")
    vcr = r.get("verified_claim_rate")
    vcr_str = f"{vcr:.0%}" if vcr is not None else "-"
    print(
        f"  [{r['case_id']:<20}] {status:<22} route={route:<14}{route_ok} "
        f"recall={recall_str:>4} judge={judge_str} gnd={gnd_str} vcr={vcr_str:>5} "
        f"iter={r.get('react_iterations',0)} tok={r.get('react_tokens_used',0):,}"
    )


def _print_summary(s: dict) -> None:
    print(f"\n{'='*70}")
    print(f"v2 Eval Summary  ({s['total']} cases, {s.get('errors',0)} errors)")
    scored_n = s.get("recall_scored_n", 0)
    if scored_n:
        print(f"  Recall@10:           {s.get('avg_recall_at_10', 0):.1%}  "
              f"(over {scored_n} cases with ground-truth)")
    else:
        print(f"  Recall@10:           n/a (no cases with ground truth)")
    print(f"  Traceability:        {s.get('avg_traceability', 0):.1%}")
    print(f"  Route accuracy:      {s['route_accuracy']:.1%}" if s.get('route_accuracy') is not None else "  Route accuracy:      n/a")
    print(f"  Fault-kind acc:      {s['fault_kind_accuracy']:.1%}" if s.get('fault_kind_accuracy') is not None else "  Fault-kind acc:      n/a")
    print(f"  Root-cause acc:      {s['root_cause_accuracy']:.1%}" if s.get('root_cause_accuracy') is not None else "  Root-cause acc:      n/a")
    print(f"")
    print(f"  ── Grounding (v2.1 P0-1 anti-overconfidence metrics) ──────────")
    print(f"  Grounded rate:       {s['grounded_rate']:.1%} (of diagnosed)" if s.get('grounded_rate') is not None else "  Grounded rate:       n/a")
    print(f"  Speculation rate:    {s['speculation_rate']:.1%} (of diagnosed)" if s.get('speculation_rate') is not None else "  Speculation rate:    n/a")
    print(f"  Abstention rate:     {s['abstention_rate']:.1%} (of all)" if s.get('abstention_rate') is not None else "  Abstention rate:     n/a")
    print(f"  ★ Grounded+Correct:  {s['grounded_correct_rate']:.1%} (judge ✓ AMONG grounded — strictest)" if s.get('grounded_correct_rate') is not None else "  ★ Grounded+Correct:  n/a")
    print(f"  Avg verified-claim:  {s['avg_verified_claim_rate']:.1%}" if s.get('avg_verified_claim_rate') is not None else "  Avg verified-claim:  n/a")
    print(f"  Judge uncertain:     {s['judge_uncertain_rate']:.1%} (dual judges disagreed)" if s.get('judge_uncertain_rate') is not None else "  Judge uncertain:     n/a")
    print(f"")
    # v2.4 KG-path coverage (Goal-1 lens — how thoroughly the agent
    # used the available knowledge graph paths).
    cov = s.get("avg_kg_path_coverage")
    cov_n = s.get("kg_coverage_scored_n", 0)
    if cov is not None:
        print(f"")
        print(f"  ── KG-path coverage (v2.4 Goal-1: did agent use available paths?) ─")
        print(f"  KG path coverage:    {cov:.1%}  "
              f"(over {cov_n} cases with expected_kg_paths)")
        # Also break out by primary_goal so Goal-1 vs Goal-3 don't wash out
        per_goal = s.get("per_goal") or {}
        print(f"")
        print(f"  ── Per primary_goal (v2.4 three-goal lens) ─────────────────────")
        for goal, d in sorted(per_goal.items()):
            gc = d.get("grounded_correct_rate")
            gc_s = f"grounded+correct={gc:.0%}" if gc is not None else "g+c=n/a"
            kgcov = d.get("avg_kg_path_coverage")
            kgcov_s = f"kg_cov={kgcov:.0%}" if kgcov is not None else "kg_cov=n/a"
            rec = d.get("avg_recall_at_10")
            rec_s = f"recall={rec:.0%}" if rec is not None else "recall=n/a"
            print(f"    {goal:<22} n={d['n']:>2} diagnosed={d['diagnosed']:>2}/{d['n']} "
                  f"{gc_s:<24} {kgcov_s:<14} {rec_s}")

    print(f"")
    print(f"  Avg latency:         {s.get('avg_elapsed_ms', 0)/1000:.1f}s")
    print(f"  Avg ReAct iters:     {s.get('avg_react_iterations', 0):.1f}")
    print(f"  Avg tokens used:     {s.get('avg_react_tokens', 0):,.0f}")
    verdicts = s.get("verdicts", {})
    if verdicts:
        print(f"  Verdicts:            {verdicts}")
    cats = s.get("per_category", {})
    if cats:
        print(f"  Per category:")
        for cat, data in cats.items():
            rec = data.get("avg_recall")
            rec_str = (f"recall={rec:.0%} (n={data['recall_scored_n']})"
                       if rec is not None else "recall=n/a")
            print(f"    {cat:<16} n={data['count']} {rec_str} route={data['route_correct_pct']:.0%}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
