"""Bug recall route — BM25 search over bugzilla bug table."""

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
        SELECT b.id,
               b.title,
               b.description,
               b.status,
               b.severity,
               ts_rank_cd(body_tsv, query) AS score
          FROM bug b,
               websearch_to_tsquery('english', :q) AS query
         WHERE body_tsv @@ query
         ORDER BY score DESC
         LIMIT :lim
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": tsq, "lim": query.limit_per_route}).fetchall()
    except Exception as exc:
        logger.error("Bug recall SQL failed: %s", exc)
        return []

    return [
        Evidence(
            route=RouteTag.bug,
            score=float(row.score or 0),
            title=row.title or "",
            body=(row.description or "")[:500],
            bug_id=row.id,
            metadata={"status": row.status, "severity": row.severity},
        )
        for row in rows
    ]


def _to_tsquery(tokens: list[str]) -> str:
    words = [w for t in tokens for w in t.split()]
    clean = [w.replace("'", "") for w in words if len(w) >= 2]
    return " OR ".join(clean[:15]) if clean else ""
