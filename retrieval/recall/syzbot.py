"""Syzbot recall route — matches crash title and call trace via BM25.

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
            table="syzbot_crash sc",
            rank_sql="ts_rank_cd(body_tsv, q)",
            select_sql="sc.syzbot_id, sc.title, sc.stack_trace",
            keywords=keywords,
            limit=query.limit_per_route,
        )

    if not rows:
        return []
    logger.debug("[recall syzbot] tier=%d (%d rows) q=%r", tier, len(rows), used_q)

    return [
        Evidence(
            route=RouteTag.syzbot,
            score=float(row.score or 0),
            title=row.title or "",
            body=(row.stack_trace or "")[:500],
            crash_id=row.syzbot_id,
            metadata={"recall_tier": tier},
        )
        for row in rows
    ]
