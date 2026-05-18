"""LKML ingester — orchestrates fetcher, parser, thread_builder, summarizer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from ingest.base import BaseIngester, Quarantine, RunReport
from ingest.lkml.fetcher import LoreFetcher, KERNEL_LISTS
from ingest.lkml.parser import ParsedMessage, parse_mbox_bytes
from ingest.lkml.thread_builder import ThreadBuilder
from ingest.lkml.summarizer import ThreadSummarizer

logger = logging.getLogger(__name__)

# ADR-010: start from 2024-05-14
_BOOTSTRAP_SINCE = datetime(2024, 5, 14, tzinfo=timezone.utc)


class LkmlIngester(BaseIngester):
    source_name = "lkml"

    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        last_per_list: dict[str, str] = checkpoint.get("last_message_id_per_list", {})
        since_per_list: dict[str, datetime] = checkpoint.get("since_per_list", {})
        deferred_summaries: list[int] = checkpoint.get("summary_deferred_queue", [])

        from configs.config import get_config
        cfg = get_config()
        lists = cfg.ingestion.lkml.lists if hasattr(cfg.ingestion, "lkml") else KERNEL_LISTS

        fetcher = LoreFetcher(self._data_dir)
        thread_builder = ThreadBuilder(self._engine)
        summarizer = ThreadSummarizer(self._engine)

        new_since_per_list: dict[str, str] = {}

        for list_name in lists:
            try:
                since_iso = since_per_list.get(list_name)
                since_dt = (
                    datetime.fromisoformat(since_iso)
                    if since_iso
                    else _BOOTSTRAP_SINCE
                )
                data = fetcher.fetch_mbox(list_name, since=since_dt)
                if not data.strip():
                    logger.info("[lkml] %s: no new messages", list_name)
                    new_since_per_list[list_name] = since_dt.isoformat()
                    continue

                messages = parse_mbox_bytes(data, list_name)
                logger.info("[lkml] %s: parsed %d messages", list_name, len(messages))

                newest_date: datetime | None = None
                for pm in messages:
                    try:
                        thread_id = thread_builder.get_or_create_thread(pm)
                        self._upsert_message(pm, thread_id, report, quarantine)
                        if pm.date and (newest_date is None or pm.date > newest_date):
                            newest_date = pm.date
                    except Exception as exc:
                        quarantine.put(pm.message_id, pm.body[:200], str(exc))
                        report.rows_failed += 1

                new_since_per_list[list_name] = (
                    newest_date.isoformat() if newest_date else since_dt.isoformat()
                )

                # Save mbox to disk (use current month as filename)
                fetcher.save_mbox(list_name, data, datetime.now(timezone.utc))

            except Exception as exc:
                logger.error("[lkml] list %s failed: %s", list_name, exc)
                new_since_per_list[list_name] = since_per_list.get(list_name, "")

        # Run pending LLM summaries (consume deferred + new eligible threads)
        threads_to_summarize = deferred_summaries[:]
        threads_to_summarize += thread_builder.find_threads_needing_summary()
        new_deferred: list[int] = []
        for tid in threads_to_summarize[:20]:  # cap per run to protect Pro budget
            ok = summarizer.summarize_thread(tid)
            if not ok:
                new_deferred.append(tid)

        return {
            "since_per_list": new_since_per_list,
            "last_message_id_per_list": last_per_list,
            "summary_deferred_queue": new_deferred,
        }

    def _upsert_message(
        self,
        pm: ParsedMessage,
        thread_id: int,
        report: RunReport,
        quarantine: Quarantine,
    ) -> None:
        sql = text("""
            INSERT INTO lkml_message
                (message_id, thread_id, in_reply_to, author_name, author_email, date, subject, body)
            VALUES
                (:mid, :tid, :irt, :an, :ae, :date, :subj, :body)
            ON CONFLICT (message_id) DO NOTHING
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {
                    "mid": pm.message_id,
                    "tid": thread_id,
                    "irt": pm.in_reply_to,
                    "an": pm.author_name,
                    "ae": pm.author_email,
                    "date": pm.date,
                    "subj": pm.subject,
                    "body": pm.body,
                })
                conn.commit()
            if result.rowcount > 0:
                report.rows_inserted += 1

            # Patch row
            if pm.is_patch:
                self._upsert_patch(pm, report)

            # Review rows
            for rv in pm.reviews:
                self._upsert_review(pm.message_id, rv, pm.date, report, quarantine)

        except Exception as exc:
            quarantine.put(pm.message_id, pm.body[:200], str(exc))
            report.rows_failed += 1

    def _upsert_patch(self, pm: ParsedMessage, report: RunReport) -> None:
        sql = text("""
            INSERT INTO lkml_patch (message_id, patch_number, series_total)
            VALUES (:mid, :num, :total)
            ON CONFLICT (message_id) DO NOTHING
        """)
        with self._engine.connect() as conn:
            conn.execute(sql, {
                "mid": pm.message_id,
                "num": pm.patch_number,
                "total": pm.series_total,
            })
            conn.commit()

    def _upsert_review(
        self,
        message_id: str,
        rv: dict,
        date: datetime | None,
        report: RunReport,
        quarantine: Quarantine,
    ) -> None:
        from email.utils import parseaddr
        name, email_addr = parseaddr(rv.get("reviewer", ""))
        sql = text("""
            INSERT INTO lkml_review (message_id, review_type, reviewer_name, reviewer_email, date)
            VALUES (:mid, :rtype, :rname, :remail, :date)
            ON CONFLICT (message_id, review_type, reviewer_email) DO NOTHING
        """)
        try:
            with self._engine.connect() as conn:
                conn.execute(sql, {
                    "mid": message_id,
                    "rtype": rv.get("review_type", ""),
                    "rname": name,
                    "remail": email_addr,
                    "date": date,
                })
                conn.commit()
        except Exception as exc:
            quarantine.put(message_id, rv, str(exc))
