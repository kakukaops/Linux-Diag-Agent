"""L2 knowledge-graph query tools (v2 T-007 / T-008).

Two commit-graph queries the diagnosis agent leans on for its core verdict:
  - check_backport_status — "is this upstream fix in my OLK kernel?"
  - get_regression_fixes  — "does this commit itself have a known regression?"

Both are read-only SQL over `kernel_commit`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


def check_backport_status(upstream_sha: str, olk_version: str) -> dict[str, Any]:
    """Whether a mainline commit has been backported to an OLK kernel version.

    Args:
        upstream_sha: mainline commit SHA — short or full.
        olk_version:  "OLK-6.6" / "OLK-5.10" (also accepts "v6.6" / "v5.10").

    Returns:
        {backported: bool, ...}. When backported, also olk_commit_hash /
        olk_commit_subject / inclusion_type.
    """
    from storage.pg.engine import get_engine

    sha = (upstream_sha or "").strip().lower()
    version = _normalize_olk_version(olk_version)
    if not sha:
        return {"backported": False, "error": "empty upstream_sha"}

    sql = text("""
        SELECT hash, subject, olk_inclusion_type
          FROM kernel_commit
         WHERE upstream_commit LIKE :prefix
           AND :ver = ANY(affected_versions)
         LIMIT 1
    """)
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                sql, {"prefix": sha[:12] + "%", "ver": version}
            ).fetchone()
    except Exception as exc:
        logger.error("check_backport_status SQL failed: %s", exc)
        return {"backported": False, "error": str(exc)}

    if row is None:
        return {"backported": False, "upstream_sha": upstream_sha,
                "olk_version": version}
    return {
        "backported": True,
        "upstream_sha": upstream_sha,
        "olk_version": version,
        "olk_commit_hash": row.hash,
        "olk_commit_subject": row.subject or "",
        "inclusion_type": row.olk_inclusion_type,   # 'mainline' | 'stable' | ...
    }


def get_regression_fixes(commit_hash: str) -> dict[str, Any]:
    """Commits that fix a regression introduced by *commit_hash*.

    Finds commits whose `Fixes:` trailer references the given commit — i.e. the
    given commit caused a bug a later commit had to fix. Critical before
    recommending a backport: a fix that itself has a known regression must not
    be recommended alone.

    Returns {commit_hash, has_regression_fix: bool, fixing_commits: [...]}.
    """
    from storage.pg.engine import get_engine

    h = (commit_hash or "").strip().lower()
    if not h:
        return {"commit_hash": commit_hash, "has_regression_fix": False,
                "fixing_commits": [], "error": "empty commit_hash"}

    engine = get_engine()
    full = _resolve_full_hash(engine, h) or h

    # A commit X "fixes a regression in C" when an element of X.fixes_refs is a
    # prefix of C's full hash. Most `Fixes:` refs are abbreviated SHAs.
    sql = text("""
        SELECT hash, subject, olk_inclusion_type
          FROM kernel_commit
         WHERE fixes_refs IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM unnest(fixes_refs) AS fr
                WHERE fr <> '' AND :full LIKE fr || '%'
           )
         ORDER BY commit_date
         LIMIT 20
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"full": full}).fetchall()
    except Exception as exc:
        logger.error("get_regression_fixes SQL failed: %s", exc)
        return {"commit_hash": full, "has_regression_fix": False,
                "fixing_commits": [], "error": str(exc)}

    fixing = [
        {"hash": r.hash, "subject": r.subject or "",
         "inclusion_type": r.olk_inclusion_type}
        for r in rows
    ]
    return {
        "commit_hash": full,
        "has_regression_fix": bool(fixing),
        "fixing_commits": fixing,
    }


# ── helpers ─────────────────────────────────────────────────────────────────

def _normalize_olk_version(v: str) -> str:
    """'v6.6' / '6.6' → 'OLK-6.6'; 'OLK-*' passes through unchanged."""
    v = (v or "").strip()
    return {
        "v6.6": "OLK-6.6", "6.6": "OLK-6.6",
        "v5.10": "OLK-5.10", "5.10": "OLK-5.10",
    }.get(v, v)


def _resolve_full_hash(engine, partial: str) -> str | None:
    """Resolve a (possibly short) hash to a full kernel_commit.hash."""
    sql = text("SELECT hash FROM kernel_commit WHERE hash LIKE :p LIMIT 1")
    with engine.connect() as conn:
        row = conn.execute(sql, {"p": partial + "%"}).fetchone()
    return row[0] if row else None
