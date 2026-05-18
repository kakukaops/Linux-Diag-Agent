"""Code recall route — delegates to CodeGraph HTTP MCP client."""

from __future__ import annotations

import logging

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    from clients.codegraph.client import get_codegraph_client, CodeGraphError

    client = get_codegraph_client()
    if not client.health_check():
        logger.warning("CodeGraph unavailable; code route returns empty")
        return []

    q = " ".join(query.keywords) if query.keywords else query.raw_question
    try:
        hits = client.search_code(
            q,
            version_hint=query.kernel_version,
            limit=query.limit_per_route,
        )
    except CodeGraphError as exc:
        logger.error("CodeGraph search_code failed: %s", exc)
        return []

    results: list[Evidence] = []
    for h in hits:
        results.append(Evidence(
            route=RouteTag.code,
            score=float(h.get("score", 0.0)),
            title=h.get("file", ""),
            body=h.get("snippet", ""),
            metadata={k: v for k, v in h.items() if k not in ("score", "file", "snippet")},
        ))
    return results
