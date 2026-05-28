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

    # Pre-v2.3 had `new_ids[:200]` hard cap per run + scraped only
    # /upstream. Combined effect: 999 rows ingested out of ~10.5K
    # available on syzbot dashboards. v2.3 removes the cap and walks
    # all three dashboards.
    _MAX_PER_RUN = 2000   # courtesy cap so a single run doesn't hammer
                          # syzbot for 4 h straight at 1.5s/req; weekly
                          # cron + idempotent NOT EXISTS picks up the rest

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

        # Skip crashes we've seen UNLESS their dashboard says "fixed":
        # transitioning from open → fixed must be re-fetched to capture
        # the Fix: commit hash that just appeared.
        new_ids: list[tuple[str, str]] = []
        for c in crashes:
            cid = c["id"]
            if cid in seen_ids and c.get("dashboard") != "fixed":
                continue
            new_ids.append((cid, c.get("dashboard", "open")))

        # Fixed-dashboard crashes come first (per scraper sort), and they
        # carry the most valuable data (Fix: commit).
        n_fixed = sum(1 for _, d in new_ids if d == "fixed")
        logger.info(
            "[syzbot] %d candidate crashes (fixed=%d, open/invalid=%d)",
            len(new_ids), n_fixed, len(new_ids) - n_fixed,
        )

        for crash_id, _ in new_ids[: self._MAX_PER_RUN]:
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
        # v2.3: also write fix_commits (jsonb) — the FULL list of fix SHAs
        # including stable backports, parallel to cve.fix_commits. The
        # primary fix_commit column stays as fix_commits[0] for back-compat.
        import json
        fix_list_json = json.dumps(crash.fix_commit_list) if crash.fix_commit_list else None
        sql = text("""
            INSERT INTO syzbot_crash
                (syzbot_id, title, status, fix_commit, fix_commits,
                 stack_trace, stack_signature)
            VALUES
                (:sid, :title, :status, :fix, CAST(:fix_list AS jsonb),
                 :trace, :sig)
            ON CONFLICT (syzbot_id) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                fix_commit = COALESCE(EXCLUDED.fix_commit, syzbot_crash.fix_commit),
                fix_commits = COALESCE(EXCLUDED.fix_commits, syzbot_crash.fix_commits),
                stack_signature = COALESCE(EXCLUDED.stack_signature, syzbot_crash.stack_signature)
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {
                    "sid": crash.syzbot_id,
                    "title": crash.title,
                    "status": crash.status,
                    "fix": crash.fix_commit,
                    "fix_list": fix_list_json,
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
