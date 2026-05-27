"""Shared tsquery helpers for PG-backed recall routes (v2.2 P0 Path B).

The OR-priority recall (v2.0) produces flooded candidate pools where
`ts_rank_cd` over-weights repeated-term documents, ranking the actual
target commit at #65, #364, #1185 in oom-001 — far below the 100-row
reranker cap. Path B uses a tiered AND fallback:

  AND(k1..kN) → AND(k1..kN-1) → ... → AND(k1) → OR(k1..kN)

until ≥ MIN_AND_HITS candidates are found OR only one keyword remains.
This bypasses ts_rank_cd dilution by SHRINKING the candidate set to
the genuinely-multi-keyword-matching documents, which the reranker
can then resolve cheaply.

Single-source helpers used by commit / lkml / bug / cve / syzbot recall.
"""

from __future__ import annotations

import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

# If AND with current keyword set returns fewer hits than this, drop the
# rightmost keyword and retry. Calibrated so the reranker (cap=60) still
# has room to choose AND hits + cross-graph injections.
MIN_AND_HITS = 3

# Hard floor; below this many keywords we stop AND-tiering and switch to OR.
MIN_AND_KEYWORDS = 1


def _clean(tokens: list[str]) -> list[str]:
    """Strip quotes, drop tokens < 2 chars, drop duplicates preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        for w in (t or "").split():
            w = w.replace("'", "").replace('"', "").strip()
            if len(w) >= 2 and w.lower() not in seen:
                seen.add(w.lower())
                out.append(w)
    return out[:15]   # 15-keyword ceiling (tsquery has cost limits)


def or_tsquery(tokens: list[str]) -> str:
    """OR-join — used as last-resort fallback."""
    clean = _clean(tokens)
    return " OR ".join(clean) if clean else ""


def and_tsquery(tokens: list[str]) -> str:
    """Space-join — websearch_to_tsquery treats unquoted multi-word as AND."""
    clean = _clean(tokens)
    return " ".join(clean) if clean else ""


def tiered_and_query(
    conn,
    table: str,
    rank_sql: str,
    select_sql: str,
    keywords: list[str],
    limit: int,
    extra_where: str = "",
    extra_params: dict | None = None,
) -> tuple[list, str, int]:
    """Run AND-priority recall with progressive keyword-drop fallback.

    Args:
      conn: SQLAlchemy connection.
      table: source table (e.g. "kernel_commit")
      rank_sql: ranking expression, e.g. "ts_rank_cd(body_tsv, q)"
      select_sql: column list for SELECT (without leading SELECT)
      keywords: parsed query keywords
      limit: max rows per route
      extra_where: optional additional WHERE clauses (e.g. version filter),
                   joined with `AND`. Must NOT contain leading `AND`.
      extra_params: dict of bind params used by extra_where.

    Returns:
      (rows, query_used, tier) where tier is the # of keywords used (or 0
      if all attempts returned no rows and OR was used too).
    """
    extra_params = extra_params or {}
    clean = _clean(keywords)
    if not clean:
        return [], "", 0

    base_where = "body_tsv @@ q" + (f" AND {extra_where}" if extra_where else "")

    # Tier 1..N: AND with progressively fewer keywords
    n = len(clean)
    while n >= MIN_AND_KEYWORDS:
        q_str = " ".join(clean[:n])
        sql = text(f"""
            SELECT {select_sql}, {rank_sql} AS score
              FROM {table}, websearch_to_tsquery('english', :q) AS q
             WHERE {base_where}
             ORDER BY score DESC
             LIMIT :lim
        """)
        params = {"q": q_str, "lim": limit, **extra_params}
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.error("[recall %s] AND-%d SQL failed: %s", table, n, exc)
            rows = []
        if len(rows) >= MIN_AND_HITS:
            logger.debug("[recall %s] AND-%d hit (%d rows) q=%r",
                         table, n, len(rows), q_str)
            return list(rows), q_str, n
        n -= 1

    # Fallback to OR
    or_q = or_tsquery(clean)
    if not or_q:
        return [], "", 0
    sql = text(f"""
        SELECT {select_sql}, {rank_sql} AS score
          FROM {table}, websearch_to_tsquery('english', :q) AS q
         WHERE {base_where}
         ORDER BY score DESC
         LIMIT :lim
    """)
    params = {"q": or_q, "lim": limit, **extra_params}
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        logger.error("[recall %s] OR fallback failed: %s", table, exc)
        return [], or_q, 0
    logger.debug("[recall %s] OR-fallback (%d rows)", table, len(rows))
    return list(rows), or_q, 0  # tier 0 = OR fallback
