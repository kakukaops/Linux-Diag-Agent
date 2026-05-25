"""CVE recall route — exact CVE-ID lookup + BM25 on description."""

from __future__ import annotations

import logging

from sqlalchemy import text

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    from storage.pg.engine import get_engine

    engine = get_engine()
    results: list[Evidence] = []

    try:
        with engine.connect() as conn:
            # Exact CVE-ID lookup (highest priority)
            if query.cve_ids:
                rows = conn.execute(
                    text("""
                        SELECT cve_id, description, cvss_v3_score, published_at
                          FROM cve
                         WHERE cve_id = ANY(:ids)
                    """),
                    {"ids": [c.upper() for c in query.cve_ids]},
                ).fetchall()
                for row in rows:
                    results.append(Evidence(
                        route=RouteTag.cve,
                        score=1.0,
                        title=row.cve_id,
                        body=(row.description or "")[:500],
                        cve_id=row.cve_id,
                        metadata={
                            "cvss_score": str(row.cvss_v3_score or ""),
                            "published_at": str(row.published_at or ""),
                        },
                    ))

            # BM25 fallback via pre-built body_tsv GIN index
            if len(results) < query.limit_per_route:
                tsq = _to_tsquery(query.keywords or query.raw_question.split())
                if tsq:
                    remaining = query.limit_per_route - len(results)
                    rows = conn.execute(
                        text("""
                            SELECT cve_id,
                                   description,
                                   cvss_v3_score,
                                   ts_rank_cd(body_tsv, query) AS score
                              FROM cve,
                                   websearch_to_tsquery('english', :q) AS query
                             WHERE body_tsv @@ query
                             ORDER BY score DESC
                             LIMIT :lim
                        """),
                        {"q": tsq, "lim": remaining},
                    ).fetchall()
                    for row in rows:
                        results.append(Evidence(
                            route=RouteTag.cve,
                            score=float(row.score or 0),
                            title=row.cve_id,
                            body=(row.description or "")[:500],
                            cve_id=row.cve_id,
                            metadata={"cvss_score": str(row.cvss_v3_score or "")},
                        ))
    except Exception as exc:
        logger.error("CVE recall SQL failed: %s", exc)

    return results


def _to_tsquery(tokens: list[str]) -> str:
    words = [w for t in tokens for w in t.split()]
    clean = [w.replace("'", "") for w in words if len(w) >= 2]
    return " OR ".join(clean[:15]) if clean else ""
