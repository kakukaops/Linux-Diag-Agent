"""M3 Ingester base framework — RunReport, checkpoint, quarantine, disk guard.

All ingester modules subclass or compose with BaseIngester.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tenacity
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Disk guard: abort if free space falls below this threshold
_DISK_ABORT_FRACTION = 0.10    # 10%
_DISK_WARN_FRACTION = 0.20     # 20%


# ── RunReport ─────────────────────────────────────────────────────────────

@dataclass
class RunReport:
    """Accumulates stats for one ingester invocation."""
    source: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: str = "running"          # running | ok | error
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_failed: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def finish(self, ok: bool = True, error: str | None = None) -> None:
        self.finished_at = datetime.now(timezone.utc)
        self.status = "ok" if ok else "error"
        self.error_message = error

    def elapsed_seconds(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def summary(self) -> str:
        return (
            f"[{self.source}:{self.run_id}] {self.status} "
            f"in {self.elapsed_seconds():.1f}s — "
            f"+{self.rows_inserted} rows, ~{self.rows_updated} updated, "
            f"{self.rows_failed} failed"
        )


# ── Checkpoint helpers ────────────────────────────────────────────────────

class CheckpointStore:
    """Read/write checkpoint JSONB from ingest_runs table."""

    def __init__(self, engine, source: str) -> None:
        self._engine = engine
        self._source = source

    def load(self) -> dict[str, Any]:
        """Return the most recent successful checkpoint for this source."""
        sql = text("""
            SELECT checkpoint FROM ingest_runs
             WHERE ingester = :src AND status = 'ok'
             ORDER BY finished_at DESC
             LIMIT 1
        """)
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"src": self._source}).fetchone()
        if not row or not row[0]:
            return {}
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def save(self, report: RunReport) -> None:
        """Upsert the ingest_runs row for this report."""
        sql = text("""
            INSERT INTO ingest_runs
                (ingester, started_at, finished_at, status,
                 rows_inserted, rows_updated, checkpoint, error_message)
            VALUES
                (:src, :started, :finished, :status,
                 :inserted, :updated, CAST(:checkpoint AS jsonb), :err)
        """)
        with self._engine.connect() as conn:
            conn.execute(sql, {
                "src": self._source,
                "started": report.started_at,
                "finished": report.finished_at,
                "status": report.status,
                "inserted": report.rows_inserted,
                "updated": report.rows_updated,
                "checkpoint": json.dumps(report.checkpoint),
                "err": report.error_message,
            })
            conn.commit()


# ── Quarantine ────────────────────────────────────────────────────────────

class Quarantine:
    """Persist unparseable / failed items to data/quarantine/<source>/<run_id>/."""

    def __init__(self, data_dir: str | Path, source: str, run_id: str) -> None:
        self._base = Path(data_dir) / "quarantine" / source / run_id
        self._base.mkdir(parents=True, exist_ok=True)
        self._count = 0

    def put(self, item_id: str, raw: Any, reason: str) -> None:
        self._count += 1
        fname = self._base / f"{self._count:06d}_{item_id[:40]}.json"
        try:
            fname.write_text(json.dumps({
                "item_id": item_id,
                "reason": reason,
                "raw": raw if isinstance(raw, str) else repr(raw),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # quarantine must not raise

    @property
    def count(self) -> int:
        return self._count


# ── Disk guard ────────────────────────────────────────────────────────────

def check_disk(data_dir: str | Path) -> None:
    """Raise RuntimeError if free disk space is critically low."""
    usage = shutil.disk_usage(str(data_dir))
    free_frac = usage.free / usage.total
    if free_frac < _DISK_ABORT_FRACTION:
        raise RuntimeError(
            f"Disk critically low: {free_frac:.1%} free at {data_dir}. "
            "Aborting ingestion to prevent data corruption."
        )
    if free_frac < _DISK_WARN_FRACTION:
        logger.warning("Disk usage warning: only %.1f%% free at %s", free_frac * 100, data_dir)


# ── Retry decorator ───────────────────────────────────────────────────────

def with_retry(
    max_attempts: int = 3,
    initial_wait: float = 2.0,
    max_wait: float = 60.0,
):
    """tenacity retry decorator for network errors."""
    return tenacity.retry(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(multiplier=initial_wait, max=max_wait),
        retry=tenacity.retry_if_exception_type((OSError, ConnectionError)),
        reraise=True,
    )


# ── Base ingester ─────────────────────────────────────────────────────────

class BaseIngester(ABC):
    """Abstract base for all M3 ingesters."""

    source_name: str  # override in subclass, e.g. 'lkml', 'bugzilla'

    def __init__(self, engine=None, data_dir: str | None = None) -> None:
        if engine is None:
            from storage.pg.engine import get_engine
            engine = get_engine()
        self._engine = engine

        if data_dir is None:
            from configs.config import get_config
            data_dir = get_config().app.data_dir
        self._data_dir = Path(data_dir)
        self._checkpoint_store = CheckpointStore(engine, self.source_name)

    def run(self) -> RunReport:
        """Main entry point: load checkpoint → incremental → save report."""
        check_disk(self._data_dir)
        checkpoint = self._checkpoint_store.load()
        report = RunReport(source=self.source_name)
        quarantine = Quarantine(self._data_dir, self.source_name, report.run_id)

        logger.info("[%s] Starting incremental run %s", self.source_name, report.run_id)
        try:
            new_checkpoint = self.incremental(checkpoint, report, quarantine)
            report.checkpoint = new_checkpoint
            report.rows_failed = quarantine.count
            report.finish(ok=True)
            logger.info(report.summary())
        except Exception as exc:
            report.finish(ok=False, error=str(exc))
            logger.error("[%s] Run %s failed: %s", self.source_name, report.run_id, exc,
                         exc_info=True)
        finally:
            try:
                self._checkpoint_store.save(report)
            except Exception as exc:
                logger.error("[%s] Failed to save checkpoint: %s", self.source_name, exc)

        return report

    @abstractmethod
    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        """Execute an incremental sync.

        Returns: updated checkpoint dict (to be persisted on success).
        Side effects: write rows to PG via self._engine; update report counters.
        """
        ...

    # ── Convenience helpers ──────────────────────────────────────────────

    def _upsert_rows(
        self,
        table: str,
        rows: list[dict[str, Any]],
        conflict_cols: list[str],
        update_cols: list[str],
        report: RunReport,
        quarantine: Quarantine,
        item_id_key: str = "id",
    ) -> None:
        """Generic upsert helper — subclasses can call this for simple tables."""
        if not rows:
            return
        for row in rows:
            try:
                with self._engine.connect() as conn:
                    # Build raw SQL for upsert (simplified — subclasses use ORM for complex cases)
                    cols = list(row.keys())
                    placeholders = ", ".join(f":{c}" for c in cols)
                    conflict = ", ".join(conflict_cols)
                    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                    sql = text(f"""
                        INSERT INTO {table} ({', '.join(cols)})
                        VALUES ({placeholders})
                        ON CONFLICT ({conflict}) DO UPDATE SET {update_set}
                    """)
                    result = conn.execute(sql, row)
                    conn.commit()
                    if result.rowcount == 1:
                        report.rows_inserted += 1
                    else:
                        report.rows_updated += 1
            except Exception as exc:
                item_id = str(row.get(item_id_key, "unknown"))
                quarantine.put(item_id, row, str(exc))
                report.rows_failed += 1
