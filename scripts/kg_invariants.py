"""KG Invariants — fast structural checks on the knowledge graph.

Runs every check against the live DB and exits non-zero on the first
violation. Total runtime: seconds (no sampling, just count queries).

Each check has the shape:
  - name: short identifier
  - query: SQL returning a single integer (count of violating rows)
  - expected: the threshold (usually 0)
  - description: what this guards against — including the historical
    incident that motivated the check, where applicable

Usage:
  python scripts/kg_invariants.py            # report all, exit 1 on any violation
  python scripts/kg_invariants.py --quiet    # only print failures
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class Invariant:
    name: str
    sql: str
    expected_max: int        # violation if observed > expected_max
    description: str


# ── Checks ────────────────────────────────────────────────────────────────────

CHECKS: list[Invariant] = [
    # Node-table invariants
    Invariant(
        name="kernel_commit.hash uniqueness",
        sql="""
            SELECT count(*) FROM (
                SELECT hash FROM kernel_commit GROUP BY hash HAVING count(*) > 1
            ) t
        """,
        expected_max=0,
        description="kernel_commit PK must not have duplicates. The 2025 "
                    "git-wrapper hash-corruption bug (1.2M poisoned rows) "
                    "would trip this.",
    ),
    Invariant(
        name="kernel_commit.subsystem has no whitespace",
        sql="""
            SELECT count(*) FROM kernel_commit WHERE subsystem LIKE '% %'
        """,
        expected_max=0,
        description="The 2026-05 trailer-parser bug wrote 366K rows of "
                    "'Link: https://lore...' into subsystem. Whitespace = "
                    "pollution; real file-path subsystems never contain it.",
    ),
    Invariant(
        name="kernel_commit.upstream_commit is 40-hex or NULL",
        sql=r"""
            SELECT count(*) FROM kernel_commit
             WHERE upstream_commit IS NOT NULL
               AND upstream_commit !~ '^[0-9a-f]{40}$'
        """,
        expected_max=0,
        description="Malformed upstream_commit poisons OLK↔mainline graph "
                    "traversal (no DB-level CHECK constraint enforces this).",
    ),
    Invariant(
        name="kernel_commit.affected_versions populated for olk-origin",
        sql="""
            SELECT count(*) FROM kernel_commit
             WHERE origin = 'olk'
               AND (affected_versions IS NULL OR cardinality(affected_versions) = 0)
        """,
        expected_max=0,
        description="OLK commits must carry at least one OLK-version tag "
                    "for backport-routing & version-aware retrieval.",
    ),

    # Link-table FK closure
    Invariant(
        name="link_commit_cve.commit_hash FK closure",
        sql="""
            SELECT count(*) FROM link_commit_cve lcc
             WHERE NOT EXISTS (
                SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcc.commit_hash
             )
        """,
        expected_max=0,
        description="Every link_commit_cve row must reference a real commit.",
    ),
    Invariant(
        name="link_commit_bug.commit_hash FK closure",
        sql="""
            SELECT count(*) FROM link_commit_bug lcb
             WHERE NOT EXISTS (
                SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcb.commit_hash
             )
        """,
        expected_max=0,
        description="Every link_commit_bug row must reference a real commit.",
    ),
    Invariant(
        name="link_commit_bug.bug_id FK closure",
        sql="""
            SELECT count(*) FROM link_commit_bug lcb
             WHERE NOT EXISTS (
                SELECT 1 FROM bug b WHERE b.id = lcb.bug_id
             )
        """,
        expected_max=0,
        description="Every link_commit_bug.bug_id must point at a real bug.id "
                    "(the auto-increment PK). Pre-v2.3 the linker confused "
                    "bug.id and bug.external_id — would trip if reintroduced.",
    ),
    Invariant(
        name="link_commit_message.commit_hash FK closure",
        sql="""
            SELECT count(*) FROM link_commit_message lcm
             WHERE NOT EXISTS (
                SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcm.commit_hash
             )
        """,
        expected_max=0,
        description="Every link_commit_message row must reference a real commit.",
    ),
    Invariant(
        name="link_commit_message.message_id FK closure",
        sql="""
            SELECT count(*) FROM link_commit_message lcm
             WHERE NOT EXISTS (
                SELECT 1 FROM lkml_message lm WHERE lm.message_id = lcm.message_id
             )
        """,
        expected_max=0,
        description="Every link_commit_message row must reference a cached "
                    "lkml_message — orphan refs indicate stale state.",
    ),
    Invariant(
        name="link_commit_fixes both hashes resolve",
        sql="""
            SELECT count(*) FROM link_commit_fixes lcf
             WHERE NOT EXISTS (SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcf.fixer_hash)
                OR NOT EXISTS (SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcf.fixed_hash)
        """,
        expected_max=0,
        description="Patch-lineage edges must point to real commits on both "
                    "sides — fixer and fixed both in kernel_commit.",
    ),
    Invariant(
        name="link_commit_revert both hashes resolve",
        sql="""
            SELECT count(*) FROM link_commit_revert lcr
             WHERE NOT EXISTS (SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcr.reverter_hash)
                OR NOT EXISTS (SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcr.reverted_hash)
        """,
        expected_max=0,
        description="Revert-chain edges must point to real commits on both sides.",
    ),
    Invariant(
        name="link_syzbot_commit.commit_hash FK closure",
        sql="""
            SELECT count(*) FROM link_syzbot_commit lsc
             WHERE NOT EXISTS (
                SELECT 1 FROM kernel_commit kc WHERE kc.hash = lsc.commit_hash
             )
        """,
        expected_max=0,
        description="Every link_syzbot_commit row must reference a real commit.",
    ),
    Invariant(
        name="link_syzbot_commit.syzbot_id FK closure",
        sql="""
            SELECT count(*) FROM link_syzbot_commit lsc
             WHERE NOT EXISTS (
                SELECT 1 FROM syzbot_crash sc WHERE sc.syzbot_id = lsc.syzbot_id
             )
        """,
        expected_max=0,
        description="Every link_syzbot_commit row must reference a real syzbot bug.",
    ),
    Invariant(
        name="link_commit_symbol.commit_hash FK closure",
        sql="""
            SELECT count(*) FROM link_commit_symbol lcs
             WHERE NOT EXISTS (
                SELECT 1 FROM kernel_commit kc WHERE kc.hash = lcs.commit_hash
             )
        """,
        expected_max=0,
        description="Every link_commit_symbol row must reference a real commit.",
    ),
    Invariant(
        name="link_commit_symbol.kind in allowed set",
        sql="""
            SELECT count(*) FROM link_commit_symbol
             WHERE kind NOT IN ('function','struct','macro','unknown','sentinel')
        """,
        expected_max=0,
        description="kind is the only categorical column on this table — must "
                    "stay in the known set or filter logic in find_commits_"
                    "touching_symbol breaks silently.",
    ),

    # Source / provenance integrity
    Invariant(
        name="link_commit_cve.source is non-NULL",
        sql="""
            SELECT count(*) FROM link_commit_cve WHERE source IS NULL
        """,
        expected_max=0,
        description="Every edge needs a provenance source (KG design "
                    "principle 2). Tighten if NULLs appear.",
    ),
    Invariant(
        name="link_commit_bug.source is non-NULL",
        sql="""
            SELECT count(*) FROM link_commit_bug WHERE source IS NULL
        """,
        expected_max=0,
        description="Provenance source required.",
    ),
    Invariant(
        name="link_commit_fixes.source is non-NULL",
        sql="""
            SELECT count(*) FROM link_commit_fixes WHERE source IS NULL
        """,
        expected_max=0,
        description="Provenance source required.",
    ),

    # Sentinel + bridge invariants
    Invariant(
        name="link_commit_symbol sentinels are uniquely shaped",
        sql="""
            SELECT count(*) FROM link_commit_symbol
             WHERE kind = 'sentinel'
               AND (symbol != '__NONE__' OR file_path != '__NONE__')
        """,
        expected_max=0,
        description="Sentinel rows must have the exact (symbol, file_path) "
                    "marker so the reverse-lookup filter works.",
    ),
    Invariant(
        name="OLK-upstream bridges have no orphan upstream SHAs",
        sql="""
            SELECT count(DISTINCT olk.upstream_commit)
              FROM kernel_commit olk
              LEFT JOIN kernel_commit main ON main.hash = olk.upstream_commit
             WHERE olk.upstream_commit IS NOT NULL
               AND olk.origin = 'olk'
               AND main.hash IS NULL
        """,
        expected_max=0,
        description="Every OLK commit's upstream_commit must resolve to a "
                    "real (or stub) kernel_commit row. _link_olk_upstream "
                    "is supposed to write a stub when the upstream isn't "
                    "in our DB — failure to do so was a v2.3 bug.",
    ),

    # CVE-side hygiene
    Invariant(
        name="cve.cvss_v3_score within valid range",
        sql="""
            SELECT count(*) FROM cve
             WHERE cvss_v3_score IS NOT NULL
               AND (cvss_v3_score < 0 OR cvss_v3_score > 10)
        """,
        expected_max=0,
        description="CVSS v3 score must be in [0, 10].",
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────


def run_check(conn, c: Invariant) -> tuple[bool, int]:
    from sqlalchemy import text
    n = conn.execute(text(c.sql)).scalar() or 0
    return (n <= c.expected_max, n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true",
                    help="only print failures")
    args = ap.parse_args()

    from storage.pg.engine import get_engine
    eng = get_engine()
    passed = 0
    failed: list[tuple[Invariant, int]] = []

    with eng.connect() as conn:
        for c in CHECKS:
            ok, observed = run_check(conn, c)
            if ok:
                passed += 1
                if not args.quiet:
                    print(f"  PASS  {c.name}")
            else:
                failed.append((c, observed))
                print(f"  FAIL  {c.name}  (observed={observed}, "
                      f"expected ≤ {c.expected_max})")
                print(f"        why: {c.description}")

    print()
    total = len(CHECKS)
    if failed:
        print(f"KG INVARIANTS: {passed}/{total} pass, {len(failed)} FAIL")
        return 1
    print(f"KG INVARIANTS: all {total} pass ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
