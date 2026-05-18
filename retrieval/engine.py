"""Retrieval engine (WBS 3.7) — always-fire-all 7-route recall + optional rerank.

Routes: code / docs / lkml / bug / syzbot / commit / cve
All routes are fired in parallel threads; failures are logged and suppressed.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from retrieval.schema import Evidence, RetrievalQuery, RetrievalResult, RouteTag
from retrieval.reranker import maybe_rerank

logger = logging.getLogger(__name__)

# Import each route's recall function lazily to avoid heavy imports at module load
_ROUTE_MODULES: dict[RouteTag, str] = {
    RouteTag.code:    "retrieval.recall.code",
    RouteTag.docs:    "retrieval.recall.docs",
    RouteTag.lkml:    "retrieval.recall.lkml",
    RouteTag.bug:     "retrieval.recall.bug",
    RouteTag.syzbot:  "retrieval.recall.syzbot",
    RouteTag.commit:  "retrieval.recall.commit",
    RouteTag.cve:     "retrieval.recall.cve",
}


def retrieve(query: RetrievalQuery) -> RetrievalResult:
    """Fire all requested routes in parallel and return merged + reranked results."""
    active_routes = query.routes or list(RouteTag)
    all_items: list[Evidence] = []
    latency: dict[str, float] = {}

    with ThreadPoolExecutor(max_workers=len(active_routes)) as pool:
        futures = {
            pool.submit(_run_route, route, query): route
            for route in active_routes
        }
        for future in as_completed(futures):
            route = futures[future]
            t0 = time.monotonic()
            try:
                items = future.result()
                latency[route.value] = round((time.monotonic() - t0) * 1000, 1)
                all_items.extend(items)
                logger.debug("Route %s: %d items", route.value, len(items))
            except Exception as exc:
                latency[route.value] = -1.0
                logger.error("Route %s failed: %s", route.value, exc)

    all_items.sort(key=lambda e: e.score, reverse=True)
    all_items, reranked = maybe_rerank(query, all_items)

    return RetrievalResult(
        query=query,
        items=all_items,
        reranked=reranked,
        latency_ms=latency,
    )


def _run_route(route: RouteTag, query: RetrievalQuery) -> list[Evidence]:
    import importlib
    mod = importlib.import_module(_ROUTE_MODULES[route])
    return mod.recall(query)
