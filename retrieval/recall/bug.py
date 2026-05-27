"""Bug recall route — BM25 search over bug table.

v2.2 P0 Path B: AND-priority via tiered_and_query (see commit.py).
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

    engine = get_engine()
    with engine.connect() as conn:
        rows, used_q, tier = tiered_and_query(
            conn,
            table="bug b",
            rank_sql="ts_rank_cd(body_tsv, q)",
            select_sql="b.id, b.title, b.description, b.status, b.severity",
            keywords=keywords,
            limit=query.limit_per_route,
        )

    if not rows:
        return []
    logger.debug("[recall bug] tier=%d (%d rows) q=%r", tier, len(rows), used_q)

    return [
        Evidence(
            route=RouteTag.bug,
            score=float(row.score or 0),
            title=row.title or "",
            body=(row.description or "")[:500],
            bug_id=row.id,
            metadata={
                "status": row.status,
                "severity": row.severity,
                "recall_tier": tier,
            },
        )
        for row in rows
    ]
