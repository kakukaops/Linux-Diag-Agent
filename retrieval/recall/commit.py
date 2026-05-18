"""Commit recall route — BM25 search over kernel_commit via body_tsv STORED column.

Optionally filtered by kernel_version (affected_versions array).
"""

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

    version_filter = ""
    params: dict = {"q": tsq, "lim": query.limit_per_route}
    if query.kernel_version:
        version_filter = "AND :ver = ANY(affected_versions)"
        params["ver"] = query.kernel_version

    sql = text(f"""
        SELECT hash,
               subject,
               body,
               author_name,
               author_date,
               ts_rank_cd(body_tsv, query) AS score
          FROM kernel_commit,
               to_tsquery('english', :q) AS query
         WHERE body_tsv @@ query
           {version_filter}
         ORDER BY score DESC
         LIMIT :lim
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        logger.error("Commit recall SQL failed: %s", exc)
        return []

    return [
        Evidence(
            route=RouteTag.commit,
            score=float(row.score or 0),
            title=row.subject or "",
            body=(row.body or "")[:500],
            commit_hash=row.hash,
            metadata={
                "author": row.author_name,
                "date": str(row.author_date) if row.author_date else None,
            },
        )
        for row in rows
    ]


def _to_tsquery(tokens: list[str]) -> str:
    clean = [t.replace("'", "").replace(":", "") for t in tokens if len(t) >= 2]
    return " | ".join(clean[:10]) if clean else ""
