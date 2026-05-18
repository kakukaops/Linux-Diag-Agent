"""Syzbot recall route — matches crash title and call trace via BM25."""

from __future__ import annotations

import logging

from sqlalchemy import text

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    from storage.pg.engine import get_engine

    engine = get_engine()
    tsq = _to_tsquery(query.keywords or query.raw_question.split())
    if not tsq:
        return []

    sql = text("""
        SELECT sc.crash_id,
               sc.title,
               sc.call_trace,
               sc.reproducer_url,
               ts_rank_cd(
                   to_tsvector('english', coalesce(sc.title,'') || ' ' || coalesce(sc.call_trace,'')),
                   query
               ) AS score
          FROM syzbot_crash sc,
               to_tsquery('english', :q) AS query
         WHERE to_tsvector('english', coalesce(sc.title,'') || ' ' || coalesce(sc.call_trace,''))
               @@ query
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
            body=(row.call_trace or "")[:500],
            crash_id=row.crash_id,
            url=row.reproducer_url or "",
        )
        for row in rows
    ]


def _to_tsquery(tokens: list[str]) -> str:
    clean = [t.replace("'", "").replace(":", "") for t in tokens if len(t) >= 2]
    return " | ".join(clean[:10]) if clean else ""
