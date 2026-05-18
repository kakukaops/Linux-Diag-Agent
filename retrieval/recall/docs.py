"""Docs recall route — BM25 search over kernel documentation (if indexed).

Falls back to CodeGraph PageIndex for documentation queries when the docs
table is empty or unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)

_DOC_EXTENSIONS = (".rst", ".txt", ".md")


def recall(query: RetrievalQuery) -> list[Evidence]:
    from storage.pg.engine import get_engine

    engine = get_engine()
    results = _pg_recall(engine, query)
    if not results:
        results = _codegraph_docs_recall(query)
    return results


def _pg_recall(engine: Any, query: RetrievalQuery) -> list[Evidence]:
    tsq = _to_tsquery(query.keywords or query.raw_question.split())
    if not tsq:
        return []
    sql = text("""
        SELECT title, body, url,
               ts_rank_cd(
                   to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,'')),
                   query
               ) AS score
          FROM kernel_doc,
               to_tsquery('english', :q) AS query
         WHERE to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,'')) @@ query
         ORDER BY score DESC
         LIMIT :lim
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": tsq, "lim": query.limit_per_route}).fetchall()
        return [
            Evidence(
                route=RouteTag.docs,
                score=float(row.score or 0),
                title=row.title or "",
                body=(row.body or "")[:500],
                url=row.url or "",
            )
            for row in rows
        ]
    except Exception:
        return []


def _codegraph_docs_recall(query: RetrievalQuery) -> list[Evidence]:
    """Use CodeGraph to search .rst/.txt documentation files."""
    try:
        from clients.codegraph.client import get_codegraph_client, CodeGraphError

        client = get_codegraph_client()
        q = " ".join(query.keywords) if query.keywords else query.raw_question
        hits = client.search_code(q, version_hint=query.kernel_version, limit=query.limit_per_route)
        doc_hits = [h for h in hits if any(h.get("file", "").endswith(ext) for ext in _DOC_EXTENSIONS)]
        return [
            Evidence(
                route=RouteTag.docs,
                score=float(h.get("score", 0.0)),
                title=h.get("file", ""),
                body=h.get("snippet", ""),
            )
            for h in doc_hits
        ]
    except Exception as exc:
        logger.debug("CodeGraph docs fallback failed: %s", exc)
        return []


def _to_tsquery(tokens: list[str]) -> str:
    clean = [t.replace("'", "").replace(":", "") for t in tokens if len(t) >= 2]
    return " | ".join(clean[:10]) if clean else ""
