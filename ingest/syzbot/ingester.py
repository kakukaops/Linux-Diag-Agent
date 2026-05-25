"""syzbot ingester — orchestrates scraper → PG writes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from ingest.base import BaseIngester, Quarantine, RunReport
from ingest.syzbot.scraper import SyzbotScraper, SyzbotCrash

logger = logging.getLogger(__name__)


class SyzbotIngester(BaseIngester):
    source_name = "syzbot"

    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        seen_ids: set[str] = set(checkpoint.get("seen_crash_ids", []))
        scraper = SyzbotScraper(self._data_dir)

        try:
            crashes = scraper.list_crashes()
        except Exception as exc:
            logger.error("[syzbot] list fetch failed: %s", exc)
            return checkpoint

        new_ids = [c["id"] for c in crashes if c["id"] not in seen_ids]
        logger.info("[syzbot] %d new crashes to process", len(new_ids))

        for crash_id in new_ids[:200]:  # cap per run
            detail = scraper.fetch_crash_detail(crash_id)
            if not detail:
                quarantine.put(crash_id, {}, "detail fetch failed")
                report.rows_failed += 1
                continue

            scraper.save_crash(detail)
            self._upsert_crash(detail, report, quarantine)
            seen_ids.add(crash_id)

        return {
            "last_crawl_at": datetime.now(timezone.utc).isoformat(),
            "seen_crash_ids": list(seen_ids),
            "seen_crash_ids_count": len(seen_ids),
        }

    def _upsert_crash(self, crash: SyzbotCrash, report: RunReport, quarantine: Quarantine) -> None:
        sql = text("""
            INSERT INTO syzbot_crash
                (syzbot_id, title, status, fix_commit, stack_trace, stack_signature)
            VALUES
                (:sid, :title, :status, :fix, :trace, :sig)
            ON CONFLICT (syzbot_id) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                fix_commit = COALESCE(EXCLUDED.fix_commit, syzbot_crash.fix_commit),
                stack_signature = COALESCE(EXCLUDED.stack_signature, syzbot_crash.stack_signature)
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {
                    "sid": crash.syzbot_id,
                    "title": crash.title,
                    "status": crash.status,
                    "fix": crash.fix_commit,
                    "trace": crash.stack_trace,
                    "sig": crash.stack_signature,
                })
                conn.commit()
            if result.rowcount > 0:
                report.rows_inserted += 1
            else:
                report.rows_updated += 1
        except Exception as exc:
            quarantine.put(crash.syzbot_id, vars(crash), str(exc))
            report.rows_failed += 1
