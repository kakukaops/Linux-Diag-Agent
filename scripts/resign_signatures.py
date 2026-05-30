"""Re-sign stack_signature columns after kg/signature.py algorithm change.

Run after editing _INTERNAL_FRAMES (Bug #86 fix 2026-05-30) so the live DB
signatures match what new ingestion / runtime queries compute.

Tables and source columns:
  syzbot_crash    stack_trace        (already a clean trace block)
  bug             description        (free-form prose — pass through
                                       extract_trace_from_text)
  lkml_message    body               (free-form email — pass through
                                       extract_trace_from_text)
  dmesg_event     call_trace         (already a clean trace block)

Idempotent — running twice is a no-op for any row whose recomputed
signature equals the stored one.
"""
from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import text

from storage.pg.engine import get_engine
from kg.signature import stack_signature, extract_trace_from_text

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resign")

_BATCH = 500


def _resign_table(table: str, pk: str, source_col: str,
                  extract: bool, dry_run: bool) -> dict:
    """Re-sign one table. Returns counts dict.

    extract=True means the source column is free-form text and we must
    run extract_trace_from_text first (bug.description / lkml_message.body).
    extract=False means the source column is already a clean trace block.
    """
    engine = get_engine()
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        logger.info("=== %s: %d rows total ===", table, total)

        scanned = 0
        recomputed = 0
        unchanged = 0
        cleared = 0
        updated = 0
        new_sig = 0
        offset = 0
        t0 = time.monotonic()

        while True:
            rows = conn.execute(text(
                f"SELECT {pk} AS pk, {source_col} AS src, "
                f"       stack_signature FROM {table} "
                f"ORDER BY {pk} LIMIT :lim OFFSET :off"
            ), {"lim": _BATCH, "off": offset}).fetchall()
            if not rows:
                break

            updates: list[dict] = []
            for r in rows:
                scanned += 1
                raw = r.src or ""
                trace = extract_trace_from_text(raw) if extract else raw
                new = stack_signature(trace or "") if trace else ""
                old = (r.stack_signature or "")
                if new == old:
                    unchanged += 1
                    continue
                recomputed += 1
                if not new and old:
                    cleared += 1
                elif new and not old:
                    new_sig += 1
                else:
                    updated += 1
                updates.append({"pk": r.pk, "sig": new or None})

            if updates and not dry_run:
                conn.execute(text(
                    f"UPDATE {table} SET stack_signature = :sig "
                    f"WHERE {pk} = :pk"
                ), updates)
                conn.commit()

            offset += _BATCH
            if scanned % 5000 == 0 or scanned == total:
                logger.info("  scanned=%d/%d updated=%d unchanged=%d "
                            "(elapsed %.1fs)", scanned, total,
                            recomputed, unchanged, time.monotonic() - t0)

        return {
            "table": table, "total": total, "scanned": scanned,
            "recomputed": recomputed, "unchanged": unchanged,
            "new_sig": new_sig, "cleared": cleared, "updated": updated,
            "elapsed_s": round(time.monotonic() - t0, 1),
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute diffs but don't write")
    ap.add_argument("--table",
                    choices=["all", "syzbot_crash", "bug",
                             "lkml_message", "dmesg_event"],
                    default="all")
    args = ap.parse_args()

    plan = [
        # (table, primary_key, source_column, needs_extract_trace_from_text)
        ("syzbot_crash", "id",       "stack_trace", False),
        ("bug",          "id",       "description", True),
        ("lkml_message", "id",       "body",        True),
        ("dmesg_event",  "event_id", "call_trace",  False),
    ]
    if args.table != "all":
        plan = [p for p in plan if p[0] == args.table]

    reports = []
    for tbl, pk, col, extract in plan:
        r = _resign_table(tbl, pk, col, extract, args.dry_run)
        reports.append(r)

    logger.info("=" * 60)
    logger.info("SUMMARY (dry_run=%s)", args.dry_run)
    for r in reports:
        logger.info(
            "  %s: scanned=%d  recomputed=%d  (new=%d  changed=%d  cleared=%d)"
            "  unchanged=%d  elapsed=%.1fs",
            r["table"], r["scanned"], r["recomputed"], r["new_sig"],
            r["updated"], r["cleared"], r["unchanged"], r["elapsed_s"],
        )


if __name__ == "__main__":
    main()
