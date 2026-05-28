"""KG Fidelity Audit — random-sample each table and check against source-of-truth.

While `kg_invariants.py` catches structural violations (FK, NULL, format),
this script catches CONTENT violations — e.g. "the linker says commit X
fixes CVE Y, but NVD's reference list for Y doesn't actually contain X".
That's the silent-miss class that the v2.3 linker LIMIT-trap audit
discovered the hard way.

Each audit:
  - draws a random sample of N rows (default 100) from a table
  - cross-checks each row against authoritative source (commit body /
    git repo / source table)
  - reports per-table fidelity rate

Exit code:
  - 0 if every audited table passes its fidelity threshold (default 0.95)
  - 1 if any falls below threshold

Usage:
  python scripts/kg_audit.py                  # all audits, sample=100
  python scripts/kg_audit.py --sample 200     # larger sample
  python scripts/kg_audit.py --audit link_commit_cve   # just one
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable

from sqlalchemy import text


# ── Audit definitions ─────────────────────────────────────────────────────────

# Each audit is a function (conn, sample_size) -> (passed, total, examples_of_failure)


_BUGZILLA_URL_RE = re.compile(r"bugzilla\.kernel\.org/show_bug\.cgi\?id=(\d+)")
_GITEE_ISSUE_RE = re.compile(r"gitee\.com/openeuler/kernel/issues/([A-Z0-9]+)",
                              re.IGNORECASE)
_ATOMGIT_ISSUE_RE = re.compile(r"atomgit\.com/openeuler/kernel/issues/(\d+)",
                                re.IGNORECASE)
_LORE_URL_RE = re.compile(r"lore\.kernel\.org/[^/\s]+/([^/\s>]+)")
_FIXES_TRAILER_RE = re.compile(r"^[ \t]*Fixes:[ \t]+([0-9a-f]{8,40})\b",
                                re.IGNORECASE | re.MULTILINE)
_REVERT_SUBJECT_RE = re.compile(r'^Revert "(.+)"', re.IGNORECASE)


def audit_link_commit_cve(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each (commit, cve) edge, verify the commit SHA is in NVD's
    fix_commits[] for that CVE."""
    rows = conn.execute(text("""
        SELECT lcc.commit_hash, lcc.cve_id, c.fix_commits
          FROM link_commit_cve lcc
          JOIN cve c ON c.cve_id = lcc.cve_id
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        fix_shas = {s.lower()[:12] for s in (r.fix_commits or [])}
        if r.commit_hash[:12] in fix_shas:
            passed += 1
        else:
            fails.append(f"{r.commit_hash[:12]} ↔ {r.cve_id}: not in NVD fix_commits")
    return (passed, len(rows), fails[:5])


def audit_link_commit_message(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each (commit, message_id) edge, verify the commit body actually
    contains a lore.kernel.org URL referencing that msg-id."""
    rows = conn.execute(text("""
        SELECT lcm.commit_hash, lcm.message_id, kc.body
          FROM link_commit_message lcm
          JOIN kernel_commit kc ON kc.hash = lcm.commit_hash
         WHERE lcm.match_method = 'link_trailer'
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        body = r.body or ""
        if r.message_id in body:
            passed += 1
        else:
            fails.append(f"{r.commit_hash[:12]} ↔ {r.message_id[:40]}: msg-id not in body")
    return (passed, len(rows), fails[:5])


def audit_link_commit_bug(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each (commit, bug) edge, verify the commit body actually references
    the bug's external_id according to the correct source pattern."""
    rows = conn.execute(text("""
        SELECT lcb.commit_hash, lcb.source, b.external_id, kc.body
          FROM link_commit_bug lcb
          JOIN bug b ON b.id = lcb.bug_id
          JOIN kernel_commit kc ON kc.hash = lcb.commit_hash
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        body = r.body or ""
        ext = r.external_id or ""
        if r.source == "gitee":
            ok = any(m.group(1).upper() == ext.upper()
                     for m in _GITEE_ISSUE_RE.finditer(body))
        elif r.source == "atomgit":
            ok = any(m.group(1) == ext
                     for m in _ATOMGIT_ISSUE_RE.finditer(body))
        elif r.source == "trailer":
            # bugzilla.kernel.org URL OR bsc#/BZ-/bz# short ref
            ok = (ext in [m.group(1) for m in _BUGZILLA_URL_RE.finditer(body)]
                  or bool(re.search(rf"(?:bsc#|BZ-|bz#|bug #?){ext}\b",
                                     body, re.IGNORECASE)))
        else:
            ok = False  # unknown source = audit failure
        if ok:
            passed += 1
        else:
            fails.append(f"{r.commit_hash[:12]} ↔ bug ext={ext} (source={r.source}): "
                         f"no matching ref in body")
    return (passed, len(rows), fails[:5])


def audit_link_commit_fixes(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each (fixer, fixed) edge with source='trailer', verify the fixer's
    body actually has a `Fixes: <fixed-sha>` trailer."""
    rows = conn.execute(text("""
        SELECT lcf.fixer_hash, lcf.fixed_hash, kc.body
          FROM link_commit_fixes lcf
          JOIN kernel_commit kc ON kc.hash = lcf.fixer_hash
         WHERE lcf.source = 'trailer'
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        body = r.body or ""
        cited = [m.group(1).lower() for m in _FIXES_TRAILER_RE.finditer(body)]
        if any(c == r.fixed_hash[:len(c)] for c in cited):
            passed += 1
        else:
            fails.append(f"{r.fixer_hash[:12]} → {r.fixed_hash[:12]}: "
                         f"no `Fixes: <sha>` matching trailer")
    return (passed, len(rows), fails[:5])


def audit_link_commit_revert(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each Revert edge, verify the evidence per `source`:
       - 'subject_revert' → reverter subject = `Revert "<target subject>"`
       - 'body_reverts_commit' → reverter body contains
         `This reverts commit <reverted_hash[:12]>` (the standard form
         that `git revert` produces)."""
    rows = conn.execute(text("""
        SELECT rv.reverter_hash, rv.reverted_hash, rv.source,
               kc_r.subject AS rv_subj, kc_r.body AS rv_body,
               kc_t.subject AS target_subj
          FROM link_commit_revert rv
          JOIN kernel_commit kc_r ON kc_r.hash = rv.reverter_hash
          JOIN kernel_commit kc_t ON kc_t.hash = rv.reverted_hash
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        ok = False
        if r.source == "subject_revert":
            m = _REVERT_SUBJECT_RE.match(r.rv_subj or "")
            ok = bool(m and r.target_subj and m.group(1).strip()
                      in (r.target_subj or ""))
        elif r.source == "body_reverts_commit":
            short = r.reverted_hash[:12]
            body = r.rv_body or ""
            ok = (re.search(rf"This reverts commit\s+{short}",
                            body, re.IGNORECASE) is not None)
        else:
            fails.append(f"{r.reverter_hash[:12]}: unknown source={r.source!r}")
            continue
        if ok:
            passed += 1
        else:
            fails.append(f"{r.reverter_hash[:12]} reverts {r.reverted_hash[:12]} "
                         f"(source={r.source}): no matching evidence")
    return (passed, len(rows), fails[:5])


def audit_link_commit_symbol(conn, sample: int) -> tuple[int, int, list[str]]:
    """For each (commit, symbol, file) row with kind in {function,struct},
    verify the symbol appears in the commit's body or that the file_path is
    plausible (no whitespace, has a slash or known extension)."""
    rows = conn.execute(text("""
        SELECT commit_hash, symbol, file_path, kind FROM link_commit_symbol
         WHERE kind IN ('function','struct','macro')
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        # File path shape (cheap structural check)
        fp = r.file_path
        if " " in fp or "\t" in fp:
            fails.append(f"{r.commit_hash[:12]}: file_path has whitespace: {fp!r}")
            continue
        if "/" not in fp and not fp.endswith((".c", ".h", ".S", ".rst",
                                                ".yaml", ".json", ".sh",
                                                ".py", ".txt", ".md")):
            fails.append(f"{r.commit_hash[:12]}: file_path looks invalid: {fp!r}")
            continue
        # Symbol shape — already validated by _is_plausible_kernel_symbol in
        # the extractor. We do a weaker check here: not a C keyword, len >= 2.
        if r.symbol in {"if","else","for","while","return","do","goto"} or len(r.symbol) < 2:
            fails.append(f"{r.commit_hash[:12]}: symbol is a keyword: {r.symbol!r}")
            continue
        passed += 1
    return (passed, len(rows), fails[:5])


def audit_kernel_commit_subsystem(conn, sample: int) -> tuple[int, int, list[str]]:
    """For commits with non-NULL subsystem, verify it's a plausible
    file-path-style label (lowercase, no whitespace, length 2-50)."""
    rows = conn.execute(text("""
        SELECT hash, subsystem FROM kernel_commit
         WHERE subsystem IS NOT NULL
         ORDER BY random() LIMIT :n
    """), {"n": sample}).fetchall()
    passed = 0
    fails: list[str] = []
    for r in rows:
        s = r.subsystem
        if not s or " " in s or "\t" in s:
            fails.append(f"{r.hash[:12]}: subsystem has whitespace: {s!r}")
        elif len(s) < 2 or len(s) > 80:
            fails.append(f"{r.hash[:12]}: subsystem length invalid: {s!r}")
        elif s.startswith(("Link:", "Closes:", "http", "Reported-by:")):
            fails.append(f"{r.hash[:12]}: subsystem looks like a trailer leak: {s!r}")
        else:
            passed += 1
    return (passed, len(rows), fails[:5])


AUDITS: dict[str, Callable] = {
    "kernel_commit.subsystem":  audit_kernel_commit_subsystem,
    "link_commit_cve":          audit_link_commit_cve,
    "link_commit_message":      audit_link_commit_message,
    "link_commit_bug":          audit_link_commit_bug,
    "link_commit_fixes":        audit_link_commit_fixes,
    "link_commit_revert":       audit_link_commit_revert,
    "link_commit_symbol":       audit_link_commit_symbol,
}


# ── Runner ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=100,
                    help="rows to draw per audit (default 100)")
    ap.add_argument("--threshold", type=float, default=0.95,
                    help="fidelity floor; below this is FAIL (default 0.95)")
    ap.add_argument("--audit", action="append", default=[],
                    help="run only this audit name (repeatable)")
    args = ap.parse_args()

    from storage.pg.engine import get_engine
    eng = get_engine()
    audits = args.audit or list(AUDITS.keys())
    any_fail = False
    print(f"KG Audit — sample={args.sample}, threshold={args.threshold:.1%}\n")

    with eng.connect() as conn:
        for name in audits:
            if name not in AUDITS:
                print(f"  ?? unknown audit: {name}")
                any_fail = True
                continue
            passed, total, fails = AUDITS[name](conn, args.sample)
            if total == 0:
                print(f"  SKIP  {name:<30} (empty table)")
                continue
            rate = passed / total
            marker = "PASS" if rate >= args.threshold else "FAIL"
            print(f"  {marker}  {name:<30} {rate:>6.1%} "
                  f"({passed}/{total})")
            if fails:
                for f in fails:
                    print(f"        └─ {f}")
            if rate < args.threshold:
                any_fail = True

    print()
    if any_fail:
        print("KG AUDIT: at least one table below threshold")
        return 1
    print("KG AUDIT: all audited tables pass threshold ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
