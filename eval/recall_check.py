"""Quick retrieval-only Recall@10 check (no LLM generation).

Calls retrieve() directly with minimal query parsing so we don't burn
LLM generation budget. Use this for fast baseline checks while
waiting for data ingestion to complete.

Usage:
    python -m eval.recall_check eval/data/cases_template.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_FAULT_DOMAIN_MAP = {
    "oom": "oom",
    "lockup": "lockup",
    "oops": "panic",
    "panic": "panic",
    "generic": None,
}


def _recall(found: set, expected: set) -> float:
    if not expected:
        return 1.0
    return len(found & expected) / len(expected)


def run(cases_path: str) -> None:
    from retrieval.engine import retrieve
    from retrieval.schema import RetrievalQuery

    cases = json.loads(Path(cases_path).read_text())

    total_recall = 0.0
    case_results: list[dict] = []

    print(f"\n{'Case':<18} {'Recall@10':>10}  {'Found/Expected'}")
    print("-" * 55)

    for case in cases:
        case_id = case["id"]
        question = case["question"]
        expected_commits = set(case.get("expected_commit_hashes", []))
        expected_bugs = {str(b) for b in case.get("expected_bug_ids", [])}
        expected_all = expected_commits | expected_bugs

        # Prefer explicit error_keywords; fall back to last 12 words of question
        # (avoids preamble words like "v6.6 OLK kernel:" that dominate question.split()[:10])
        kw = case.get("error_keywords") or question.split()[-12:]
        query = RetrievalQuery(
            raw_question=question,
            kernel_version=case.get("kernel_version"),
            fault_domain=_FAULT_DOMAIN_MAP.get(case.get("category"), None),
            subsystem_hint=None,
            error_keywords=kw,
            cve_ids=[],
            commit_hashes=[],
            keywords=kw,
        )

        try:
            result = retrieve(query)
            evidence = result.items
        except Exception as exc:
            print(f"{case_id:<18} {'ERROR':>10}  {exc}")
            case_results.append({"id": case_id, "error": str(exc)})
            continue

        found_commits = {e.commit_hash for e in evidence[:10] if e.commit_hash}
        found_bugs = {str(e.bug_id) for e in evidence[:10] if e.bug_id}
        found_all = found_commits | found_bugs

        r = _recall(found_all, expected_all)
        total_recall += r
        hit_desc = f"{len(found_all & expected_all)}/{len(expected_all)}"
        print(f"{case_id:<18} {r:>10.1%}  {hit_desc}")
        case_results.append({
            "id": case_id,
            "recall": r,
            "found_commits": list(found_commits),
            "expected_commits": list(expected_commits),
            "hits": list(found_all & expected_all),
            "misses": list(expected_all - found_all),
        })

    avg = total_recall / max(len(cases), 1)
    print("-" * 55)
    print(f"{'Average Recall@10':<18} {avg:>10.1%}")
    print()

    print("Miss detail:")
    for r in case_results:
        if r.get("misses"):
            print(f"  {r['id']}: {r['misses']}")
    if all(not r.get("misses") for r in case_results if "misses" in r):
        print("  (none)")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "eval/data/cases_template.json"
    run(path)
