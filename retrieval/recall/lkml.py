"""LKML recall route — BM25 search over lkml_message via PostgreSQL tsvector."""

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
        SELECT message_id,
               subject,
               body_preview,
               ts_rank_cd(subject_tsv || body_tsv, query) AS score
          FROM lkml_message,
               to_tsquery('english', :q) AS query
         WHERE (subject_tsv || body_tsv) @@ query
         ORDER BY score DESC
         LIMIT :lim
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": tsq, "lim": query.limit_per_route}).fetchall()
    except Exception as exc:
        logger.error("LKML recall SQL failed: %s", exc)
        return []

    return [
        Evidence(
            route=RouteTag.lkml,
            score=float(row.score or 0),
            title=row.subject or "",
            body=row.body_preview or "",
            message_id=row.message_id,
        )
        for row in rows
    ]


def _to_tsquery(tokens: list[str]) -> str:
    """Build a tsquery OR expression from token list."""
    clean = [t.replace("'", "").replace(":", "") for t in tokens if len(t) >= 2]
    return " | ".join(clean[:10]) if clean else ""
