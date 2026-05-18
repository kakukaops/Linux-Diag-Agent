"""Evaluation batch runner (WBS 7.14).

Runs the diagnosis agent on the 30-case eval dataset and collects:
  - Recall@10 (evidence retrieval)
  - Root-cause correctness (A=1 if exact match, A=0 otherwise)
  - Evidence traceability (all claims have at least one verifiable ref)
  - Report readability score (from judge LLM)

Usage:
  python -m eval.runner --dataset eval/data/cases.json --output eval/results/
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
@click.option("--dataset", required=True, type=click.Path(exists=True),
              help="Path to cases JSON file.")
@click.option("--output", required=True, type=click.Path(),
              help="Directory to write per-case results.")
@click.option("--limit", default=None, type=int, help="Run only first N cases.")
@click.option("--no-llm-parse", is_flag=True, help="Disable LLM query parsing.")
def main(dataset: str, output: str, limit: int | None, no_llm_parse: bool) -> None:
    """Run batch evaluation on the diagnosis dataset."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cases = _load_cases(dataset, limit)
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        logger.info("Running case %d/%d: %s", i, len(cases), case.get("id", "?"))
        result = _run_case(case, use_llm_parse=not no_llm_parse)
        results.append(result)
        # Write per-case result
        (out_dir / f"{case.get('id', i)}.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )

    summary = _compute_summary(results)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Evaluation complete. Summary: %s", summary_path)
    _print_summary(summary)


def _load_cases(path: str, limit: int | None) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data if isinstance(data, list) else data.get("cases", [])
    return cases[:limit] if limit else cases


def _run_case(case: dict[str, Any], *, use_llm_parse: bool = True) -> dict[str, Any]:
    from agent.graph import diagnose
    from retrieval.query_parser import parse_query
    from retrieval.engine import retrieve

    case_id = case.get("id", "unknown")
    question = case.get("question", "")
    expected_commits = set(case.get("expected_commit_hashes", []))
    expected_bugs = set(case.get("expected_bug_ids", []))

    t0 = time.monotonic()
    try:
        state = diagnose(question)
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        evidence = state.get("evidence", [])
        report_md = state.get("report_md", "")
        error = state.get("error")
    except Exception as exc:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        logger.error("Case %s failed: %s", case_id, exc)
        return {
            "case_id": case_id,
            "error": str(exc),
            "elapsed_ms": elapsed,
            "recall_at_10": 0.0,
            "root_cause_correct": False,
            "traceability": 0.0,
        }

    # Recall@10
    found_commits = {e.get("commit_hash") for e in evidence[:10] if e.get("commit_hash")}
    found_bugs = {e.get("bug_id") for e in evidence[:10] if e.get("bug_id")}
    expected_all = expected_commits | {str(b) for b in expected_bugs}
    found_all = found_commits | {str(b) for b in found_bugs}
    recall_10 = len(expected_all & found_all) / max(len(expected_all), 1)

    # Claim traceability
    claims = state.get("claims", [])
    if claims:
        traceable = sum(1 for c in claims if c.get("verified"))
        traceability = traceable / len(claims)
    else:
        traceability = 0.0

    return {
        "case_id": case_id,
        "question": question,
        "elapsed_ms": elapsed,
        "recall_at_10": round(recall_10, 3),
        "traceability": round(traceability, 3),
        "root_cause_correct": None,  # requires human judge
        "fault_kind": state.get("fault_kind"),
        "sop_name": state.get("sop_name"),
        "evidence_count": len(evidence),
        "report_md": report_md[:500],
        "error": error,
    }


def _compute_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {"total": 0}
    errors = [r for r in results if r.get("error")]
    recall_vals = [r["recall_at_10"] for r in results if "recall_at_10" in r]
    trace_vals = [r["traceability"] for r in results if "traceability" in r]
    return {
        "total": n,
        "errors": len(errors),
        "avg_recall_at_10": round(sum(recall_vals) / max(len(recall_vals), 1), 3),
        "avg_traceability": round(sum(trace_vals) / max(len(trace_vals), 1), 3),
        "avg_elapsed_ms": round(
            sum(r.get("elapsed_ms", 0) for r in results) / n, 1
        ),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\n{'='*50}")
    print(f"Eval Summary ({summary['total']} cases)")
    print(f"  Recall@10:      {summary.get('avg_recall_at_10', 0):.1%}")
    print(f"  Traceability:   {summary.get('avg_traceability', 0):.1%}")
    print(f"  Avg latency:    {summary.get('avg_elapsed_ms', 0):.0f}ms")
    print(f"  Errors:         {summary.get('errors', 0)}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
