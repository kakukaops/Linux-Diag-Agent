"""LKML L2 — read-through cache (ADR-025).

`lkml_message` is the cache. On a cache miss, the message is fetched from lore
by Message-ID (`/all/<id>/raw`), parsed, and persisted — so the cache converges
toward completeness through use, with no upfront bulk download.

`fetch_and_persist` is the shared "fetch + parse + upsert one message" core,
also used by the L1 backfill (`ingest/lkml/backfill.py`).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from ingest.base import Quarantine, RunReport
from ingest.lkml.ingester import LkmlIngester
from ingest.lkml.parser import parse_mbox_bytes
from ingest.lkml.thread_builder import ThreadBuilder

logger = logging.getLogger(__name__)


def fetch_and_persist(message_id: str, *, fetcher, ingester, thread_builder,
                      report, quarantine) -> int:
    """Fetch one message by id from lore, parse, upsert into `lkml_message`.

    Returns the number of messages persisted (0 if the message is not in
    lore's archive — 404/410, or unparseable). Shared by L1 backfill and
    L2 lazy cache so the parse/persist path is not duplicated.
    """
    raw = fetcher.fetch_raw_by_id(message_id)
    if not raw:
        return 0
    msgs = parse_mbox_bytes(raw, list_name="all")
    for pm in msgs:
        thread_id = thread_builder.get_or_create_thread(pm)
        ingester._upsert_message(pm, thread_id, report, quarantine)
    return len(msgs)


# ── read-through cache ──────────────────────────────────────────────────────

_lazy: dict[str, Any] = {}  # process-shared lazy-cache context (built once)


def _ctx() -> dict[str, Any]:
    """Lazily build the process-shared cache context.

    One LkmlIngester / LoreFetcher / ThreadBuilder / Quarantine per process,
    so repeated diagnosis-time `get_message` calls don't each rebuild them.
    """
    if not _lazy:
        from ingest.lkml.fetcher import LoreFetcher
        ingester = LkmlIngester()
        report = RunReport(source="lkml_lazy")
        _lazy["ingester"] = ingester
        _lazy["fetcher"] = LoreFetcher(ingester._data_dir)
        _lazy["thread_builder"] = ThreadBuilder(ingester._engine)
        _lazy["report"] = report
        _lazy["quarantine"] = Quarantine(ingester._data_dir, "lkml_lazy", report.run_id)
    return _lazy


def get_message(message_id: str) -> dict | None:
    """Return a message from the `lkml_message` cache, fetching it from lore on
    a cache miss (ADR-025 L2 read-through).

    Returns a dict(message_id, subject, body, date, author_name), or None if
    the message is not in lore's archive.
    """
    message_id = (message_id or "").strip().strip("<>")
    if not message_id:
        return None
    c = _ctx()
    engine = c["ingester"]._engine
    cached = _query_cached(engine, message_id)
    if cached:
        return cached
    try:
        n = fetch_and_persist(
            message_id, fetcher=c["fetcher"], ingester=c["ingester"],
            thread_builder=c["thread_builder"], report=c["report"],
            quarantine=c["quarantine"],
        )
    except Exception as exc:
        logger.warning("[lkml/lazy] fetch %s failed: %s", message_id, exc)
        return None
    return _query_cached(engine, message_id) if n else None


def ensure_cached(message_ids: list[str]) -> int:
    """Ensure each Message-ID is in the cache; fetch the missing ones.

    Returns the count newly fetched. Used to warm the cache for a known set
    (e.g. messages referenced by a retrieval result).
    """
    c = _ctx()
    engine = c["ingester"]._engine
    fetched = 0
    for mid in message_ids:
        mid = (mid or "").strip().strip("<>")
        if not mid or _query_cached(engine, mid):
            continue
        try:
            if fetch_and_persist(
                mid, fetcher=c["fetcher"], ingester=c["ingester"],
                thread_builder=c["thread_builder"], report=c["report"],
                quarantine=c["quarantine"],
            ) > 0:
                fetched += 1
        except Exception as exc:
            logger.warning("[lkml/lazy] fetch %s failed: %s", mid, exc)
    return fetched


def _query_cached(engine, message_id: str) -> dict | None:
    sql = text("""
        SELECT message_id, subject, body, date, author_name
        FROM lkml_message
        WHERE message_id = :mid
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"mid": message_id}).fetchone()
    if not row:
        return None
    return {
        "message_id": row[0],
        "subject": row[1],
        "body": row[2],
        "date": row[3],
        "author_name": row[4],
    }


# ── thread summary — on-demand read-through (ADR-025) ───────────────────────

_SUMMARY_MIN_MESSAGES = 30  # short threads are read raw, not summarized


def get_thread_summary(thread_id: int) -> dict | None:
    """Read-through cache for an LKML thread's 3-part summary (ADR-025).

    On-demand: a thread is summarized the first time it is requested (e.g. when
    the ReAct agent reads it), and only if it is long enough to need
    compression. Short threads return None — the caller should read them raw,
    which is fresher and fault-contextual.

    Returns {"problem", "solution", "outcome"} or None.
    """
    engine = _ctx()["ingester"]._engine
    row = _query_thread_summary_row(engine, thread_id)
    decision = _summary_decision(row)
    if decision == "none":
        return None
    if decision == "generate":
        try:
            _summarizer().summarize_thread(thread_id)
        except Exception as exc:
            logger.warning("[lkml/lazy] summarize thread %s failed: %s", thread_id, exc)
            return None
        row = _query_thread_summary_row(engine, thread_id)
        if _summary_decision(row) != "cached":
            return None
    return {"problem": row[1], "solution": row[2], "outcome": row[3]}


def _summary_decision(row) -> str:
    """Pure: 'none' (skip) | 'cached' (reuse) | 'generate' (summarize now).

    Decoupled from DB/LLM so the gating logic is unit-testable.
    """
    if row is None or row[0] < _SUMMARY_MIN_MESSAGES:
        return "none"
    problem = row[1]
    if problem and not problem.startswith("[summary-failed:"):
        return "cached"
    return "generate"  # NULL summary, or a stale failure marker → (re)generate


def _query_thread_summary_row(engine, thread_id: int):
    sql = text("""
        SELECT message_count, summary_problem, summary_solution, summary_outcome
        FROM lkml_thread WHERE id = :tid
    """)
    with engine.connect() as conn:
        return conn.execute(sql, {"tid": thread_id}).fetchone()


def _summarizer():
    """Lazily-built, process-shared ThreadSummarizer (LLM provider inside)."""
    if "summarizer" not in _lazy:
        from ingest.lkml.summarizer import ThreadSummarizer
        _lazy["summarizer"] = ThreadSummarizer(_ctx()["ingester"]._engine)
    return _lazy["summarizer"]
