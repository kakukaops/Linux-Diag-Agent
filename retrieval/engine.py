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

    # v2.2 P0 Path B: tier-bonus. AND-tier-N hits (kept the N most-restrictive
    # keywords) are far more relevant than tier-0 (OR fallback). Boost score
    # by tier so they surface ahead of OR-floods without overriding signal.
    for e in all_items:
        tier = (e.metadata or {}).get("recall_tier")
        if isinstance(tier, int) and tier > 0:
            e.score = e.score * (1.0 + 0.25 * tier)   # tier 3 → 1.75×, 5 → 2.25×

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


_INJECT_TOP_K_PER_SRC = 10        # only top-K hits per route inject linked commits
_INJECT_MAX_LINKS_PER_HIT = 5     # cap linked commits per source hit (bugs can
                                  # have ~100 linked commits across OLK versions
                                  # — they'd flood the reranker)


def _top_by_score(items: list[Evidence], route: RouteTag, attr: str, k: int) -> dict:
    """Return {id_value: score} for the top-K route hits ranked by score desc."""
    hits = [(getattr(e, attr), e.score)
            for e in items if e.route == route and getattr(e, attr)]
    hits.sort(key=lambda x: x[1], reverse=True)
    return {idv: sc for idv, sc in hits[:k]}


def _inject_linked_commits(items: list[Evidence]) -> list[Evidence]:
    """Surface commits that the cross-graph links to LKML / CVE / bug hits.

    Score = source evidence score × 0.9 so the injected commit ranks just below
    its source. Skips commit hashes already present.

    Capped: only top-K=10 hits per route inject; each contributes at most
    5 linked commits — prevents flooding (a single bug can link to ~100
    commits across OLK backport variants).

    Bug→commit path activated post-ADR-022: bug table now has 58K rows with
    link_commit_bug populated; OLK-specific issues (gitee / atomgit) commonly
    cite their fix commits, so a BM25 bug hit can surface relevant commits
    the BM25 commit route wouldn't rank in top-K.
    """
    from sqlalchemy import text
    from storage.pg.engine import get_engine

    message_ids = _top_by_score(items, RouteTag.lkml, "message_id", _INJECT_TOP_K_PER_SRC)
    cve_ids = _top_by_score(items, RouteTag.cve, "cve_id", _INJECT_TOP_K_PER_SRC)
    bug_ids = _top_by_score(items, RouteTag.bug, "bug_id", _INJECT_TOP_K_PER_SRC)
    # patch-lineage: commits surfaced by BM25 can pull in their Fixes/Revert chain
    commit_hashes = _top_by_score(items, RouteTag.commit, "commit_hash", _INJECT_TOP_K_PER_SRC)
    if not message_ids and not cve_ids and not bug_ids and not commit_hashes:
        return []

    existing_hashes = {e.commit_hash for e in items
                       if e.route == RouteTag.commit and e.commit_hash}
    out: list[Evidence] = []
    try:
        with get_engine().connect() as conn:
            # v2.4: every cross-graph injection query filters out
            # `[stub upstream for OLK %]` rows. Stubs are useful for graph
            # closure but useless as cited evidence (date=1970, body=NULL).
            # Run 16 cve-001 had the agent cite a stub as "the fix" — fixed
            # here at retrieval time so downstream tools and prompts never
            # see them.
            if message_ids:
                rows = conn.execute(text("""
                    SELECT lcm.commit_hash, lcm.message_id, kc.subject
                      FROM link_commit_message lcm
                      JOIN kernel_commit kc ON kc.hash = lcm.commit_hash
                     WHERE lcm.message_id = ANY(:mids)
                       AND kc.subject NOT LIKE '[stub upstream%'
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
                       AND kc.subject NOT LIKE '[stub upstream%'
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
            if bug_ids:
                # Per-bug LIMIT (window function): one OLK bug can link to
                # ~100 commits (same patch backported to OLK-5.10 / 6.6 / SP*).
                # Without partition cap, 10 bugs × 100 = 1000 injections flood
                # the reranker. Cap at 5 most-recent-by-commit-date per bug.
                rows = conn.execute(text("""
                    WITH ranked AS (
                        SELECT lcb.commit_hash, lcb.bug_id, kc.subject,
                               ROW_NUMBER() OVER (
                                   PARTITION BY lcb.bug_id
                                   ORDER BY kc.commit_date DESC NULLS LAST
                               ) AS rn
                          FROM link_commit_bug lcb
                          JOIN kernel_commit kc ON kc.hash = lcb.commit_hash
                         WHERE lcb.bug_id = ANY(:bids)
                           AND kc.subject NOT LIKE '[stub upstream%'
                    )
                    SELECT commit_hash, bug_id, subject FROM ranked WHERE rn <= :n
                """), {"bids": list(bug_ids.keys()),
                       "n": _INJECT_MAX_LINKS_PER_HIT}).fetchall()
                for r in rows:
                    if r.commit_hash in existing_hashes:
                        continue
                    existing_hashes.add(r.commit_hash)
                    out.append(Evidence(
                        route=RouteTag.commit, score=bug_ids[r.bug_id] * 0.9,
                        title=r.subject or "", body="", commit_hash=r.commit_hash,
                        metadata={"linked_from": "bug", "via_bug_id": r.bug_id},
                    ))

            # Patch-lineage paths (v2.1 P0a/P0b): commits found by BM25 pull
            # in their Fixes-chain (this commit fixes that one) and Revert-chain.
            if commit_hashes:
                # Fixes-chain: kc is the FIXER, target is the FIXED. Surface
                # both directions so agent can walk patch lineage either way.
                rows = conn.execute(text("""
                    SELECT lcf.fixer_hash AS via, lcf.fixed_hash AS target,
                           kc.subject, 'fixes_target' AS direction
                      FROM link_commit_fixes lcf
                      JOIN kernel_commit kc ON kc.hash = lcf.fixed_hash
                     WHERE lcf.fixer_hash = ANY(:hashes)
                       AND kc.subject NOT LIKE '[stub upstream%'
                    UNION ALL
                    SELECT lcf.fixed_hash AS via, lcf.fixer_hash AS target,
                           kc.subject, 'fixed_by' AS direction
                      FROM link_commit_fixes lcf
                      JOIN kernel_commit kc ON kc.hash = lcf.fixer_hash
                     WHERE lcf.fixed_hash = ANY(:hashes)
                       AND kc.subject NOT LIKE '[stub upstream%'
                    LIMIT 50
                """), {"hashes": list(commit_hashes.keys())}).fetchall()
                for r in rows:
                    if r.target in existing_hashes:
                        continue
                    existing_hashes.add(r.target)
                    out.append(Evidence(
                        route=RouteTag.commit, score=commit_hashes[r.via] * 0.85,
                        title=r.subject or "", body="", commit_hash=r.target,
                        metadata={"linked_from": f"commit_{r.direction}", "via_commit": r.via},
                    ))
                # Revert-chain
                rows = conn.execute(text("""
                    SELECT lcr.reverter_hash AS via, lcr.reverted_hash AS target,
                           kc.subject, 'reverts_target' AS direction
                      FROM link_commit_revert lcr
                      JOIN kernel_commit kc ON kc.hash = lcr.reverted_hash
                     WHERE lcr.reverter_hash = ANY(:hashes)
                       AND kc.subject NOT LIKE '[stub upstream%'
                    UNION ALL
                    SELECT lcr.reverted_hash AS via, lcr.reverter_hash AS target,
                           kc.subject, 'reverted_by' AS direction
                      FROM link_commit_revert lcr
                      JOIN kernel_commit kc ON kc.hash = lcr.reverter_hash
                     WHERE lcr.reverted_hash = ANY(:hashes)
                       AND kc.subject NOT LIKE '[stub upstream%'
                    LIMIT 20
                """), {"hashes": list(commit_hashes.keys())}).fetchall()
                for r in rows:
                    if r.target in existing_hashes:
                        continue
                    existing_hashes.add(r.target)
                    out.append(Evidence(
                        route=RouteTag.commit, score=commit_hashes[r.via] * 0.85,
                        title=r.subject or "", body="", commit_hash=r.target,
                        metadata={"linked_from": f"commit_{r.direction}", "via_commit": r.via},
                    ))
    except Exception as exc:
        logger.warning("Cross-graph commit injection failed: %s", exc)
    return out
