"""CVE recall route — exact CVE-ID lookup + BM25 on description.

v2.2 P0 Path B: BM25 portion uses AND-priority via tiered_and_query.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from retrieval.recall._tsquery import tiered_and_query
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
                            "recall_tier": "exact",
                        },
                    ))

            # BM25 AND-priority fallback for remaining slots
            remaining = query.limit_per_route - len(results)
            if remaining > 0:
                keywords = query.keywords or query.raw_question.split()
                if keywords:
                    rows, used_q, tier = tiered_and_query(
                        conn,
                        table="cve",
                        rank_sql="ts_rank_cd(body_tsv, q)",
                        select_sql="cve_id, description, cvss_v3_score",
                        keywords=keywords,
                        limit=remaining,
                    )
                    logger.debug("[recall cve] tier=%d (%d rows) q=%r",
                                 tier, len(rows), used_q)
                    for row in rows:
                        results.append(Evidence(
                            route=RouteTag.cve,
                            score=float(row.score or 0),
                            title=row.cve_id,
                            body=(row.description or "")[:500],
                            cve_id=row.cve_id,
                            metadata={
                                "cvss_score": str(row.cvss_v3_score or ""),
                                "recall_tier": tier,
                            },
                        ))
    except Exception as exc:
        logger.error("CVE recall SQL failed: %s", exc)

    return results
