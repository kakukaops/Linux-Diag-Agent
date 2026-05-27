"""Commit recall route — BM25 search over kernel_commit via body_tsv STORED column.

v2.2 P0 Path B: AND-priority recall via tiered_and_query — try AND with all
keywords, progressively drop rightmost until ≥3 hits, fall back to OR.
This avoids ts_rank_cd dilution that pushed expected commits to rank #65+
in OR-flooded pools.

Optionally filtered by kernel_version (affected_versions array).
"""

from __future__ import annotations

import logging

from retrieval.recall._tsquery import tiered_and_query
from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    from storage.pg.engine import get_engine

    keywords = query.keywords or query.raw_question.split()
    if not keywords:
        return []

    extra_where = ""
    extra_params: dict = {}
    if query.kernel_version:
        extra_where = ":ver = ANY(affected_versions)"
        extra_params["ver"] = query.kernel_version

    engine = get_engine()
    with engine.connect() as conn:
        rows, used_q, tier = tiered_and_query(
            conn,
            table="kernel_commit",
            rank_sql="ts_rank_cd(body_tsv, q)",
            select_sql="hash, subject, body, author_name, commit_date",
            keywords=keywords,
            limit=query.limit_per_route,
            extra_where=extra_where,
            extra_params=extra_params,
        )

    if not rows:
        return []
    logger.debug("[recall commit] tier=%d (%d rows) q=%r", tier, len(rows), used_q)

    return [
        Evidence(
            route=RouteTag.commit,
            score=float(row.score or 0),
            title=row.subject or "",
            body=(row.body or "")[:500],
            commit_hash=row.hash,
            metadata={
                "author": row.author_name,
                "date": str(row.commit_date) if row.commit_date else None,
                "recall_tier": tier,  # 0=OR fallback; N=AND with N keywords
            },
        )
        for row in rows
    ]
