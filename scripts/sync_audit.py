"""sync_audit.py — Health check after weekly sync (M3 §5.3).

Checks ingest_runs for staleness, failure rates, and disk usage.
Exits non-zero if any critical threshold is exceeded.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Thresholds
MAX_STALE_DAYS = 14
MAX_FAILED_ITEMS = 100
DISK_WARN_PCT = 20.0
DISK_ABORT_PCT = 10.0

SOURCES = ["lkml", "bugzilla", "syzbot", "nvd", "kernel_commit"]


def main() -> int:
    from storage.pg.engine import get_engine
    from configs.config import get_config

    cfg = get_config()
    engine = get_engine()
    data_dir = Path(cfg.app.data_dir)

    alerts: list[str] = []
    warnings: list[str] = []
    critical = False

    # ── Disk check ────────────────────────────────────────────────────────
    try:
        usage = shutil.disk_usage(str(data_dir))
        free_pct = usage.free / usage.total * 100
        if free_pct < DISK_ABORT_PCT:
            alerts.append(f"CRITICAL disk: {free_pct:.1f}% free at {data_dir}")
            critical = True
        elif free_pct < DISK_WARN_PCT:
            warnings.append(f"WARN disk: {free_pct:.1f}% free at {data_dir}")
    except Exception as e:
        warnings.append(f"WARN disk check failed: {e}")

    # ── Ingestion staleness check ─────────────────────────────────────────
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=MAX_STALE_DAYS)
    sql = text("""
        SELECT ingester,
               MAX(CASE WHEN status='ok' THEN finished_at END) AS last_success,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_runs,
               SUM(COALESCE(rows_inserted, 0)) AS total_inserted
          FROM ingest_runs
         WHERE started_at > NOW() - INTERVAL '30 days'
         GROUP BY ingester
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
    except Exception as e:
        logger.error("DB query failed: %s", e)
        return 1

    seen = {r[0] for r in rows}
    for src in SOURCES:
        if src not in seen:
            warnings.append(f"WARN {src}: no runs in last 30 days")
            continue

    for src_name, last_success, error_runs, total_inserted in rows:
        if last_success and last_success < stale_threshold:
            alerts.append(
                f"CRITICAL {src_name}: last success was {last_success.date()} "
                f"(>{MAX_STALE_DAYS} days ago)"
            )
            critical = True
        if error_runs and error_runs >= 2:
            alerts.append(f"CRITICAL {src_name}: {error_runs} consecutive error runs")
            critical = True

    # ── Report ────────────────────────────────────────────────────────────
    summary = {
        "run_at": now.isoformat(),
        "critical": critical,
        "alerts": alerts,
        "warnings": warnings,
    }

    # Write to metrics file
    metrics_dir = data_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "sync_audit.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    for msg in warnings:
        logger.warning(msg)
    for msg in alerts:
        logger.error(msg)

    if critical:
        logger.error("CRITICAL alerts detected — sync audit FAILED")
        return 1

    logger.info("Sync audit OK (%d warnings)", len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
