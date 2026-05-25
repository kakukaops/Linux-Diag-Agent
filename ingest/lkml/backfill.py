"""LKML reference-driven backfill — ADR-025 L1.

Fetches the LKML messages that kernel commits actually reference via their
`Link:` trailers, instead of bulk-ingesting whole lists by date window.

Why: an OLK commit's `Link:` trailer points to the original patch discussion,
typically 1-3 years old and spread across many mailing lists. Bulk ingestion
by (list, date-window) was measured to miss ~85% of them (2026-05-21). Targeted
fetch by Message-ID via lore's list-agnostic /all/ archive covers all of them
regardless of list or age — so `link_commit_message` can actually be populated.

Entry point: python -m ingest.lkml backfill_referenced [limit]

Resumable: progress is checkpointed to a JSON file under the data dir, so a run
can be interrupted (it takes ~11h for the initial ~26k messages) and resumed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ingest.base import Quarantine, RunReport
from ingest.lkml.fetcher import LoreFetcher
from ingest.lkml.ingester import LkmlIngester
from ingest.lkml.lazy import fetch_and_persist
from ingest.lkml.thread_builder import ThreadBuilder

logger = logging.getLogger(__name__)

# lore URL → Message-ID: the last path segment containing '@'.
# Matches /r/<id>, /all/<id>, /<list>/<id> alike.
_LORE_MSGID_RE = re.compile(
    r"lore\.kernel\.org/[^\s]+/([^/\s>]+@[^/\s>]+)",
    re.IGNORECASE,
)
_CHECKPOINT_FILE = "lkml_backfill_checkpoint.json"
_SAVE_EVERY = 50  # persist checkpoint every N messages (crash-safety + resume)


def extract_referenced_message_ids(engine) -> list[str]:
    """T-125: distinct lore Message-IDs referenced by kernel_commit bodies.

    Done in SQL (regexp over kernel_commit.body) — read-only, does not touch
    lkml_message, so it is safe to run alongside an active LKML ingestion.
    """
    sql = text(r"""
        SELECT DISTINCT (regexp_matches(
            body, 'lore\.kernel\.org/[^\s]+/([^/\s>]+@[^/\s>]+)', 'g'))[1] AS mid
        FROM kernel_commit
        WHERE body ~ 'lore\.kernel\.org'
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    ids = []
    for (mid,) in rows:
        mid = (mid or "").strip().strip("<>")
        if _is_valid_message_id(mid):
            ids.append(mid)
    return ids


def _is_valid_message_id(mid: str) -> bool:
    """Reject noise from regex extraction (truncated URLs, bracket fragments)."""
    if not mid or "@" not in mid:
        return False
    if len(mid) < 5 or len(mid) > 998:  # RFC 5322 line-length sanity bound
        return False
    if any(c.isspace() for c in mid):
        return False
    return True


def _checkpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / _CHECKPOINT_FILE


def _load_checkpoint(data_dir: Path) -> dict[str, Any]:
    """Load the backfill checkpoint; return an empty one if absent/corrupt.

    Kept in a standalone JSON file (not the ingest_runs table) so it does not
    collide with the normal lkml ingester's checkpoint.
    """
    p = _checkpoint_path(data_dir)
    if p.exists():
        try:
            ckpt = json.loads(p.read_text(encoding="utf-8"))
            ckpt.setdefault("fetched", [])
            ckpt.setdefault("not_found", [])
            ckpt.setdefault("last_run", None)
            return ckpt
        except Exception as exc:
            logger.warning("[lkml/backfill] checkpoint unreadable (%s), starting fresh", exc)
    return {"fetched": [], "not_found": [], "last_run": None}


def _save_checkpoint(data_dir: Path, ckpt: dict[str, Any]) -> None:
    _checkpoint_path(data_dir).write_text(
        json.dumps(ckpt, indent=2), encoding="utf-8"
    )


def backfill_referenced(*, limit: int | None = None) -> dict[str, Any]:
    """T-126/127: fetch every commit-referenced LKML message not yet cached.

    Reuses LoreFetcher (fetch-by-id), the lkml parser, ThreadBuilder and
    LkmlIngester._upsert_message — no duplicated parse/persist logic.

    Returns a summary dict.
    """
    ingester = LkmlIngester()          # reuse engine + _upsert_message
    engine = ingester._engine
    data_dir = ingester._data_dir

    referenced = extract_referenced_message_ids(engine)
    ckpt = _load_checkpoint(data_dir)
    done = set(ckpt["fetched"]) | set(ckpt["not_found"])
    todo = [m for m in referenced if m not in done]
    if limit:
        todo = todo[:limit]

    logger.info("[lkml/backfill] referenced=%d already_done=%d todo=%d",
                len(referenced), len(done), len(todo))

    report = RunReport(source="lkml_backfill")
    quarantine = Quarantine(data_dir, "lkml_backfill", report.run_id)
    thread_builder = ThreadBuilder(engine)
    fetched = inserted = not_found = failed = 0

    with LoreFetcher(data_dir) as fetcher:
        for i, mid in enumerate(todo, 1):
            try:
                n = fetch_and_persist(
                    mid, fetcher=fetcher, ingester=ingester,
                    thread_builder=thread_builder, report=report,
                    quarantine=quarantine,
                )
                if n == 0:
                    # not in lore's archive (404/410); record so we don't retry
                    ckpt["not_found"].append(mid)
                    not_found += 1
                else:
                    ckpt["fetched"].append(mid)
                    fetched += 1
                    inserted += n
            except Exception as exc:
                logger.warning("[lkml/backfill] %s failed: %s", mid, exc)
                quarantine.put(mid, "", str(exc))
                failed += 1
            if i % _SAVE_EVERY == 0:
                _save_checkpoint(data_dir, ckpt)
                logger.info("[lkml/backfill] progress %d/%d (fetched=%d not_found=%d failed=%d)",
                            i, len(todo), fetched, not_found, failed)

    ckpt["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_checkpoint(data_dir, ckpt)
    summary = {
        "referenced_total": len(referenced),
        "todo": len(todo),
        "fetched": fetched,
        "messages_inserted": inserted,
        "not_found": not_found,
        "failed": failed,
    }
    logger.info("[lkml/backfill] done: %s", summary)
    return summary
