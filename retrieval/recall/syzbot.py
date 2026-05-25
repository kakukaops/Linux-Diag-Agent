"""Syzbot recall route — matches crash title and call trace via BM25."""

from __future__ import annotations

import logging

from sqlalchemy import text

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    from storage.pg.engine import get_engine

    engine = get_engine()
    tsq = _keywords_str(query.keywords or query.raw_question.split())
    if not tsq:
        return []

    sql = text("""
        SELECT sc.syzbot_id,
               sc.title,
               sc.stack_trace,
               ts_rank_cd(body_tsv, query) AS score
          FROM syzbot_crash sc,
               websearch_to_tsquery('english', :q) AS query
         WHERE body_tsv @@ query
         ORDER BY score DESC
         LIMIT :lim
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": tsq, "lim": query.limit_per_route}).fetchall()
    except Exception as exc:
        logger.error("Syzbot recall SQL failed: %s", exc)
        return []

    return [
        Evidence(
            route=RouteTag.syzbot,
            score=float(row.score or 0),
            title=row.title or "",
            body=(row.stack_trace or "")[:500],
            crash_id=row.syzbot_id,
        )
        for row in rows
    ]


def _keywords_str(tokens: list[str]) -> str:
    words = [w for t in tokens for w in t.split()]
    clean = [w.replace("'", "") for w in words if len(w) >= 2]
    return " OR ".join(clean[:15]) if clean else ""
