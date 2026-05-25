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

    # Cross-graph injection: a relevant LKML message or CVE often links to a
    # commit that BM25 wouldn't rank in the commit-route top-K (kernel
    # commit bodies use developer terminology that user queries lack). Walk
    # the link tables we already built and inject those commits as fresh
    # commit-route candidates so the reranker can pick them up.
    all_items.extend(_inject_linked_commits(all_items))

    # Normalize each route's scores to [0, 1] before global merge so that
    # routes with differently-scaled BM25 scores (e.g. ts_rank_cd on long LKML
    # emails vs short commit bodies) don't crowd out each other.
    _normalize_by_route(all_items)
    all_items.sort(key=lambda e: e.score, reverse=True)
    all_items, reranked = maybe_rerank(query, all_items)

    return RetrievalResult(
        query=query,
        items=all_items,
        reranked=reranked,
        latency_ms=latency,
    )


def _normalize_by_route(items: list[Evidence]) -> None:
    """Max-normalize scores within each route to [0, 1] in-place."""
    from collections import defaultdict
    by_route: dict[RouteTag, list[Evidence]] = defaultdict(list)
    for e in items:
        by_route[e.route].append(e)
    for route_items in by_route.values():
        max_score = max((e.score for e in route_items), default=1.0) or 1.0
        for e in route_items:
            e.score = e.score / max_score


def _run_route(route: RouteTag, query: RetrievalQuery) -> list[Evidence]:
    import importlib
    mod = importlib.import_module(_ROUTE_MODULES[route])
    return mod.recall(query)


def _inject_linked_commits(items: list[Evidence]) -> list[Evidence]:
    """Surface commits that the cross-graph links to LKML / CVE hits.

    Score = source evidence score × 0.9 so the injected commit ranks just below
    its source. Skips commit hashes already present.
    """
    from sqlalchemy import text
    from storage.pg.engine import get_engine

    message_ids = {e.message_id: e.score for e in items
                   if e.route == RouteTag.lkml and e.message_id}
    cve_ids = {e.cve_id: e.score for e in items
               if e.route == RouteTag.cve and e.cve_id}
    if not message_ids and not cve_ids:
        return []

    existing_hashes = {e.commit_hash for e in items
                       if e.route == RouteTag.commit and e.commit_hash}
    out: list[Evidence] = []
    try:
        with get_engine().connect() as conn:
            if message_ids:
                rows = conn.execute(text("""
                    SELECT lcm.commit_hash, lcm.message_id, kc.subject
                      FROM link_commit_message lcm
                      JOIN kernel_commit kc ON kc.hash = lcm.commit_hash
                     WHERE lcm.message_id = ANY(:mids)
                """), {"mids": list(message_ids.keys())}).fetchall()
                for r in rows:
                    if r.commit_hash in existing_hashes:
                        continue
                    existing_hashes.add(r.commit_hash)
                    out.append(Evidence(
                        route=RouteTag.commit, score=message_ids[r.message_id] * 0.9,
                        title=r.subject or "", body="", commit_hash=r.commit_hash,
                        metadata={"linked_from": "lkml", "via_message_id": r.message_id},
                    ))
            if cve_ids:
                rows = conn.execute(text("""
                    SELECT lcc.commit_hash, lcc.cve_id, kc.subject
                      FROM link_commit_cve lcc
                      JOIN kernel_commit kc ON kc.hash = lcc.commit_hash
                     WHERE lcc.cve_id = ANY(:cves)
                """), {"cves": list(cve_ids.keys())}).fetchall()
                for r in rows:
                    if r.commit_hash in existing_hashes:
                        continue
                    existing_hashes.add(r.commit_hash)
                    out.append(Evidence(
                        route=RouteTag.commit, score=cve_ids[r.cve_id] * 0.9,
                        title=r.subject or "", body="", commit_hash=r.commit_hash,
                        metadata={"linked_from": "cve", "via_cve_id": r.cve_id},
                    ))
    except Exception as exc:
        logger.warning("Cross-graph commit injection failed: %s", exc)
    return out
