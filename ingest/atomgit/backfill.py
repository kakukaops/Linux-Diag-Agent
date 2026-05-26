"""Reference-driven backfill of atomgit issues cited by OLK commits (ADR-022).

Scans kernel_commit.body for atomgit.com/openeuler/kernel/issues/<N> refs,
fetches each unique numeric ID via /api/v5, upserts into bug table with
source='atomgit'. Resumable via data/atomgit_backfill_checkpoint.json.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ingest.atomgit.fetcher import AtomgitFetcher, AtomgitServerError
from ingest.atomgit.parser import parse_issue
from storage.pg.engine import get_engine

logger = logging.getLogger(__name__)

_ISSUE_ID_RE = re.compile(
    r"atomgit\.com/openeuler/kernel/issues/(\d+)",
    re.IGNORECASE,
)
_CHECKPOINT = Path("data/atomgit_backfill_checkpoint.json")
_SAVE_EVERY = 50


def extract_referenced_ids(engine) -> list[str]:
    """Distinct atomgit issue numeric IDs cited in kernel_commit.body."""
    sql = text(r"""
        SELECT DISTINCT (regexp_matches(
            body, 'atomgit\.com/openeuler/kernel/issues/(\d+)', 'g'))[1] AS mid
          FROM kernel_commit
         WHERE body ILIKE '%atomgit.com/openeuler/kernel/issues%'
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return sorted({r.mid for r in rows}, key=int)


def _load_ckpt() -> dict[str, Any]:
    if _CHECKPOINT.exists():
        try:
            data = json.loads(_CHECKPOINT.read_text(encoding="utf-8"))
            data.setdefault("server_error", [])
            return data
        except Exception as exc:
            logger.warning("checkpoint unreadable, starting fresh: %s", exc)
    return {"fetched": [], "not_found": [], "server_error": [], "last_run": None}


def _save_ckpt(ckpt: dict[str, Any]) -> None:
    _CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT.write_text(json.dumps(ckpt, indent=2), encoding="utf-8")


def _upsert_bug(conn, row: dict[str, Any]) -> None:
    conn.execute(text("""
        INSERT INTO bug (source, external_id, title, status, component,
                         subsystem, severity, reporter, assignee,
                         created_at, updated_at, closed_at,
                         kernel_versions, description)
        VALUES (:source, :external_id, :title, :status, :component,
                :subsystem, :severity, :reporter, :assignee,
                :created_at, :updated_at, :closed_at,
                :kernel_versions, :description)
        ON CONFLICT (source, external_id) DO UPDATE SET
            title           = EXCLUDED.title,
            status          = EXCLUDED.status,
            updated_at      = EXCLUDED.updated_at,
            closed_at       = EXCLUDED.closed_at,
            kernel_versions = EXCLUDED.kernel_versions,
            description     = EXCLUDED.description
    """), row)


def backfill(*, limit: int | None = None, token: str | None = None) -> dict[str, Any]:
    """Fetch every cited atomgit issue, upsert into bug. Skips already-done."""
    engine = get_engine()
    ckpt = _load_ckpt()
    fetched_set = set(ckpt["fetched"])
    not_found_set = set(ckpt["not_found"])
    server_err_set = set(ckpt["server_error"])
    seen_done = fetched_set | not_found_set | server_err_set

    all_ids = extract_referenced_ids(engine)
    todo = [i for i in all_ids if i not in seen_done]
    if limit:
        todo = todo[:limit]
    logger.info("[atomgit] %d total cited, %d already done (ok=%d 404=%d 5xx=%d), %d to fetch",
                len(all_ids), len(seen_done), len(fetched_set),
                len(not_found_set), len(server_err_set), len(todo))

    n_ok = n_404 = n_5xx = n_err = 0
    def _flush():
        ckpt["fetched"] = sorted(fetched_set, key=int)
        ckpt["not_found"] = sorted(not_found_set, key=int)
        ckpt["server_error"] = sorted(server_err_set, key=int)
        ckpt["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_ckpt(ckpt)

    with AtomgitFetcher(token=token) as fetcher:
        for i, iid in enumerate(todo, 1):
            try:
                raw = fetcher.get_issue(iid)
            except AtomgitServerError as exc:
                logger.warning("[atomgit] %s server error (skipped): %s", iid, exc)
                server_err_set.add(iid)
                n_5xx += 1
                if i % _SAVE_EVERY == 0:
                    _flush()
                continue
            except Exception as exc:
                logger.error("[atomgit] %s transient failure: %s", iid, exc)
                n_err += 1
                continue
            if raw is None:
                not_found_set.add(iid)
                n_404 += 1
            else:
                row = parse_issue(raw)
                try:
                    with engine.begin() as conn:
                        _upsert_bug(conn, row)
                    fetched_set.add(iid)
                    n_ok += 1
                except Exception as exc:
                    logger.error("[atomgit] %s upsert failed: %s", iid, exc)
                    n_err += 1
                    continue
            if i % _SAVE_EVERY == 0:
                _flush()
                logger.info("[atomgit] progress %d/%d  ok=%d 404=%d 5xx=%d err=%d",
                            i, len(todo), n_ok, n_404, n_5xx, n_err)
    _flush()
    return {"fetched": n_ok, "not_found": n_404, "server_error": n_5xx,
            "errors": n_err, "total_cited": len(all_ids)}
