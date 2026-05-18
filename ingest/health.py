"""Ingestion pipeline health monitor + self-healing (WBS 11.8).

Self-healing strategies:
  1. Circuit breaker: pause ingester after N consecutive failures
  2. Exponential backoff cooldown before retry
  3. Quarantine replay: attempt to re-process quarantined items
  4. Staleness alert: warn if source hasn't succeeded in >14 days

Run by weekly_sync.sh or as a standalone cron.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Circuit breaker thresholds
CIRCUIT_OPEN_AFTER = 3          # consecutive error runs
COOLDOWN_BASE_MINUTES = 30      # exponential backoff base
COOLDOWN_MAX_MINUTES = 480      # 8h cap
STALE_DAYS = 14                 # warn if last success older than this


@dataclass
class IngesterHealth:
    source: str
    consecutive_errors: int = 0
    last_success: datetime | None = None
    last_error: datetime | None = None
    last_error_message: str | None = None
    circuit_open: bool = False
    cooldown_until: datetime | None = None

    @property
    def is_stale(self) -> bool:
        if self.last_success is None:
            return True
        return (datetime.now(timezone.utc) - self.last_success).days > STALE_DAYS

    @property
    def can_run(self) -> bool:
        if not self.circuit_open:
            return True
        if self.cooldown_until and datetime.now(timezone.utc) > self.cooldown_until:
            return True
        return False


def get_ingester_health(engine: Any, sources: list[str]) -> dict[str, IngesterHealth]:
    """Query ingest_runs and compute current health per source."""
    sql = text("""
        SELECT ingester,
               status,
               finished_at,
               error_message,
               started_at
          FROM ingest_runs
         WHERE started_at > NOW() - INTERVAL '30 days'
         ORDER BY ingester, started_at DESC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()

    # Group by ingester, ordered by recency
    from collections import defaultdict
    by_source: dict[str, list] = defaultdict(list)
    for r in rows:
        by_source[r[0]].append(r)

    health: dict[str, IngesterHealth] = {}
    for src in sources:
        runs = by_source.get(src, [])
        h = IngesterHealth(source=src)

        # Find last success
        for r in runs:
            if r[1] == "ok" and r[2]:
                h.last_success = r[2]
                break

        # Count consecutive errors from most recent
        for r in runs:
            if r[1] == "error":
                h.consecutive_errors += 1
                if h.last_error is None:
                    h.last_error = r[2]
                    h.last_error_message = r[3]
            else:
                break  # hit a success, stop counting

        # Circuit breaker logic
        if h.consecutive_errors >= CIRCUIT_OPEN_AFTER:
            h.circuit_open = True
            backoff_m = min(
                COOLDOWN_BASE_MINUTES * (2 ** (h.consecutive_errors - CIRCUIT_OPEN_AFTER)),
                COOLDOWN_MAX_MINUTES,
            )
            if h.last_error:
                h.cooldown_until = h.last_error + timedelta(minutes=backoff_m)
            logger.warning(
                "[%s] Circuit open: %d consecutive errors. Cooldown until %s",
                src, h.consecutive_errors,
                h.cooldown_until.isoformat() if h.cooldown_until else "unknown",
            )

        health[src] = h
    return health


def run_health_check(engine: Any, sources: list[str]) -> dict[str, Any]:
    """Run health check and return a JSON-serializable report."""
    health = get_ingester_health(engine, sources)
    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources": {},
    }
    any_critical = False

    for src, h in health.items():
        status = "ok"
        issues: list[str] = []

        if h.circuit_open and not h.can_run:
            status = "circuit_open"
            issues.append(f"Circuit open: {h.consecutive_errors} consecutive errors")
            any_critical = True
        elif h.consecutive_errors > 0:
            status = "degraded"
            issues.append(f"{h.consecutive_errors} consecutive errors")

        if h.is_stale:
            status = status if status != "ok" else "stale"
            issues.append(f"No successful run in >{STALE_DAYS} days")
            any_critical = True

        report["sources"][src] = {
            "status": status,
            "consecutive_errors": h.consecutive_errors,
            "last_success": h.last_success.isoformat() if h.last_success else None,
            "last_error": h.last_error.isoformat() if h.last_error else None,
            "can_run": h.can_run,
            "issues": issues,
        }
        for issue in issues:
            logger.warning("[health] %s: %s", src, issue)

    report["any_critical"] = any_critical
    return report


# ── Quarantine replay ─────────────────────────────────────────────────────────


def replay_quarantine(
    source: str,
    quarantine_dir: str | Path,
    ingester_class: type,
    *,
    max_items: int = 100,
) -> int:
    """Re-process items from quarantine for a given source.

    Returns the count of successfully replayed items.
    """
    q_base = Path(quarantine_dir) / "quarantine" / source
    if not q_base.exists():
        logger.info("No quarantine directory for %s", source)
        return 0

    replayed = 0
    run_dirs = sorted(q_base.iterdir(), reverse=True)  # most recent first

    for run_dir in run_dirs:
        if replayed >= max_items:
            break
        files = list(run_dir.glob("*.json"))
        for f in files[:max_items - replayed]:
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
                logger.debug("Replaying quarantine item %s from %s", f.name, source)
                # Subclasses must implement replay_item() for quarantine re-processing
                ingester = ingester_class()
                if hasattr(ingester, "replay_item"):
                    ingester.replay_item(item["raw"], item["item_id"])
                    f.rename(f.with_suffix(".replayed"))
                    replayed += 1
            except Exception as exc:
                logger.warning("Quarantine replay failed for %s: %s", f.name, exc)

    logger.info("Quarantine replay for %s: %d items processed", source, replayed)
    return replayed


# ── CLI entry ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from storage.pg.engine import get_engine
    from configs.config import get_config

    cfg = get_config()
    engine = get_engine()
    sources = ["lkml", "bugzilla", "syzbot", "nvd", "kernel_commit"]
    report = run_health_check(engine, sources)

    metrics_dir = Path(cfg.app.data_dir) / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "ingest_health.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    sys.exit(1 if report["any_critical"] else 0)
