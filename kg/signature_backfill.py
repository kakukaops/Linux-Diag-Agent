"""Backfill stack_signature for existing bug + lkml_message rows.

For each row with a non-empty body, try to extract a trace block and
hash it. Sets stack_signature to NULL when no trace can be extracted
(distinct from "unprocessed" — we record the result so we don't redo
the regex pass).

Usage:
  python -m kg.signature_backfill              # process both tables
  python -m kg.signature_backfill --table bug
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def backfill_bug(engine, batch: int = 2000) -> dict:
    """Backfill bug.stack_signature. Returns counts."""
    from sqlalchemy import text
    from kg.signature import extract_trace_from_text, stack_signature

    stats = {"scanned": 0, "with_trace": 0, "without_trace": 0}
    while True:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, description FROM bug
                 WHERE stack_signature IS NULL
                   AND description IS NOT NULL
                 LIMIT :n
            """), {"n": batch}).fetchall()
        if not rows:
            break
        updates: list[dict] = []
        for r in rows:
            stats["scanned"] += 1
            trace = extract_trace_from_text(r.description or "")
            if trace:
                sig = stack_signature(trace)
                updates.append({"id": r.id, "sig": sig})
                stats["with_trace"] += 1
            else:
                updates.append({"id": r.id, "sig": ""})  # empty sentinel
                stats["without_trace"] += 1
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE bug SET stack_signature = :sig WHERE id = :id
            """), updates)
        logger.info("[backfill bug] scanned=%d with_trace=%d without=%d",
                    stats["scanned"], stats["with_trace"], stats["without_trace"])
        if len(rows) < batch:
            break
    return stats


def backfill_lkml(engine, batch: int = 2000) -> dict:
    """Backfill lkml_message.stack_signature. Only updates rows whose
    body contains the call-trace marker; saves a regex pass otherwise."""
    from sqlalchemy import text
    from kg.signature import extract_trace_from_text, stack_signature

    stats = {"scanned": 0, "with_trace": 0, "without_trace": 0}
    while True:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT message_id, body FROM lkml_message
                 WHERE stack_signature IS NULL
                   AND body IS NOT NULL
                   AND length(body) > 200
                 LIMIT :n
            """), {"n": batch}).fetchall()
        if not rows:
            break
        updates: list[dict] = []
        for r in rows:
            stats["scanned"] += 1
            trace = extract_trace_from_text(r.body or "")
            if trace:
                sig = stack_signature(trace)
                updates.append({"mid": r.message_id, "sig": sig})
                stats["with_trace"] += 1
            else:
                updates.append({"mid": r.message_id, "sig": ""})
                stats["without_trace"] += 1
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE lkml_message SET stack_signature = :sig
                 WHERE message_id = :mid
            """), updates)
        logger.info("[backfill lkml] scanned=%d with_trace=%d without=%d",
                    stats["scanned"], stats["with_trace"], stats["without_trace"])
        if len(rows) < batch:
            break
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", choices=("bug", "lkml", "both"), default="both")
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    from storage.pg.engine import get_engine
    eng = get_engine()
    if args.table in ("bug", "both"):
        s = backfill_bug(eng, args.batch)
        print(f"bug:   {s}")
    if args.table in ("lkml", "both"):
        s = backfill_lkml(eng, args.batch)
        print(f"lkml:  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
