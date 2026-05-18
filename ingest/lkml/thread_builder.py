"""LKML thread DAG builder — incrementally links messages into threads."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from ingest.lkml.parser import ParsedMessage

logger = logging.getLogger(__name__)


class ThreadBuilder:
    """Resolves Message-ID → Thread-ID relationships in the PG lkml_thread table."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def get_or_create_thread(self, msg: ParsedMessage) -> int:
        """Return thread_id for *msg*, creating a new thread row if needed."""
        # 1. Try to find thread by in_reply_to chain
        parent_thread_id = self._find_thread_by_parent(msg)
        if parent_thread_id:
            self._update_thread_stats(parent_thread_id, msg.date)
            return parent_thread_id

        # 2. Also check references (fallback)
        for ref_id in reversed(msg.references or []):
            tid = self._find_thread_by_message_id(ref_id)
            if tid:
                self._update_thread_stats(tid, msg.date)
                return tid

        # 3. Create new thread
        return self._create_thread(msg)

    def _find_thread_by_parent(self, msg: ParsedMessage) -> int | None:
        if not msg.in_reply_to:
            return None
        return self._find_thread_by_message_id(msg.in_reply_to)

    def _find_thread_by_message_id(self, message_id: str) -> int | None:
        sql = text("""
            SELECT t.id FROM lkml_thread t
            JOIN lkml_message m ON m.thread_id = t.id
            WHERE m.message_id = :mid
            LIMIT 1
        """)
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"mid": message_id}).fetchone()
        return row[0] if row else None

    def _create_thread(self, msg: ParsedMessage) -> int:
        sql = text("""
            INSERT INTO lkml_thread (root_message_id, subject, list_name, date_start, message_count)
            VALUES (:root_id, :subject, :list_name, :date_start, 0)
            RETURNING id
        """)
        with self._engine.connect() as conn:
            row = conn.execute(sql, {
                "root_id": msg.message_id,
                "subject": msg.subject or "(no subject)",
                "list_name": msg.list_name,
                "date_start": msg.date or datetime.now(timezone.utc),
            }).fetchone()
            conn.commit()
        return row[0]

    def _update_thread_stats(self, thread_id: int, msg_date: datetime | None) -> None:
        sql = text("""
            UPDATE lkml_thread
               SET message_count = message_count + 1,
                   date_end = GREATEST(date_end, :d)
             WHERE id = :tid
        """)
        with self._engine.connect() as conn:
            conn.execute(sql, {
                "tid": thread_id,
                "d": msg_date or datetime.now(timezone.utc),
            })
            conn.commit()

    def find_threads_needing_summary(self, min_messages: int = 30, silence_days: int = 14) -> list[int]:
        """Return thread IDs eligible for LLM summarization (M1 §6 D6)."""
        sql = text("""
            SELECT id FROM lkml_thread
             WHERE message_count >= :min
               AND (date_end IS NULL OR date_end < NOW() - INTERVAL ':silence days')
               AND summary_problem IS NULL
             ORDER BY message_count DESC
             LIMIT 100
        """)
        # Interpolate interval manually (psycopg3 doesn't bind interval literals)
        sql = text(f"""
            SELECT id FROM lkml_thread
             WHERE message_count >= :min
               AND (date_end IS NULL OR date_end < NOW() - INTERVAL '{silence_days} days')
               AND summary_problem IS NULL
             ORDER BY message_count DESC
             LIMIT 100
        """)
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"min": min_messages}).fetchall()
        return [r[0] for r in rows]
