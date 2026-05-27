"""LKML recall route — hybrid L2 cache BM25 + L3 lore live search (ADR-025).

Two sub-sources, merged:
  L2  local BM25 over the `lkml_message` cache (PostgreSQL body_tsv).
  L3  lore.kernel.org live full-text search — only in `online` mode; covers
      the full archive (all lists, all history), not just the cached subset.

Offline (air-gapped) mode uses L2 only. L3 failures degrade gracefully — the
route always returns at least the local results.

Note: L3-discovered messages are NOT fetched in full here (that would add a
per-hit network round-trip to retrieval). The lore search snippet is used as
the Evidence body; the L2 cache is populated lazily when the agent later reads
a specific message via `ingest.lkml.lazy.get_message`.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from retrieval.schema import Evidence, RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)


def recall(query: RetrievalQuery) -> list[Evidence]:
    """Hybrid LKML recall: L2 cache BM25 + (online) L3 lore search, merged."""
    local = _local_bm25(query)
    remote = _lore_recall(query) if _knowledge_mode() == "online" else []
    return _merge(local, remote, query.limit_per_route)


# ── L2 — local cache BM25 ───────────────────────────────────────────────────

def _local_bm25(query: RetrievalQuery) -> list[Evidence]:
    """v2.2 P0 Path B: AND-priority via tiered_and_query."""
    from retrieval.recall._tsquery import tiered_and_query
    from storage.pg.engine import get_engine

    keywords = query.keywords or query.raw_question.split()
    if not keywords:
        return []

    with get_engine().connect() as conn:
        rows, used_q, tier = tiered_and_query(
            conn,
            table="lkml_message",
            rank_sql="ts_rank_cd(body_tsv, q)",
            select_sql="message_id, subject, body",
            keywords=keywords,
            limit=query.limit_per_route,
        )

    if not rows:
        return []
    logger.debug("[recall lkml/L2] tier=%d (%d rows) q=%r", tier, len(rows), used_q)
    return [
        Evidence(
            route=RouteTag.lkml,
            score=float(row.score or 0),
            title=row.subject or "",
            body=(row.body or "")[:500],
            message_id=row.message_id,
            metadata={"source": "l2_cache", "recall_tier": tier},
        )
        for row in rows
    ]


# ── L3 — lore live search ───────────────────────────────────────────────────

def _lore_recall(query: RetrievalQuery) -> list[Evidence]:
    from retrieval.recall.lore_search import search

    kws = _search_keywords(query)
    if not kws:
        return []
    hits = search(kws, limit=query.limit_per_route)
    n = len(hits)
    return [
        Evidence(
            route=RouteTag.lkml,
            # rank-based descending score; _merge re-normalizes per sub-source
            score=1.0 - (i / n) if n else 1.0,
            title=hit.title,
            body=hit.snippet,
            message_id=hit.message_id,
            metadata={"source": "lore_search", "permalink": hit.permalink},
        )
        for i, hit in enumerate(hits)
    ]


# ── merge ───────────────────────────────────────────────────────────────────

def _merge(local: list[Evidence], remote: list[Evidence], limit: int) -> list[Evidence]:
    """Merge the two sub-sources: max-normalize each to [0,1] (so local
    ts_rank_cd and lore rank scores become comparable), dedup by message_id
    (keep the local hit — it carries the full cached body)."""
    _normalize(local)
    _normalize(remote)
    by_id: dict[str, Evidence] = {}
    for ev in local:
        by_id[ev.message_id] = ev
    for ev in remote:
        existing = by_id.get(ev.message_id)
        if existing is None:
            by_id[ev.message_id] = ev
        elif ev.score > existing.score:
            existing.score = ev.score  # lift score, keep local body
    merged = sorted(by_id.values(), key=lambda e: e.score, reverse=True)
    return merged[:limit]


def _normalize(evs: list[Evidence]) -> None:
    """Max-normalize scores to [0, 1] in-place."""
    if not evs:
        return
    mx = max((e.score for e in evs), default=1.0) or 1.0
    for e in evs:
        e.score = e.score / mx


# ── helpers ─────────────────────────────────────────────────────────────────

def _knowledge_mode() -> str:
    try:
        from configs.config import get_config
        return get_config().knowledge.mode
    except Exception:
        return "online"


def _keywords_str(tokens: list[str]) -> str:
    """websearch_to_tsquery OR-string for the local BM25 query."""
    words = [w for t in tokens for w in t.split()]
    clean = [w.replace("'", "") for w in words if len(w) >= 2]
    return " OR ".join(clean[:15]) if clean else ""


def _search_keywords(query: RetrievalQuery) -> str:
    """Plain space-joined keyword string for lore's Xapian search."""
    tokens = query.keywords or query.raw_question.split()
    clean = [w for w in tokens if len(w) >= 2]
    return " ".join(clean[:12])
