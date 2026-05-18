"""LKML long-thread LLM summarizer (M3 §3.1, M1 Navigator backend).

Generates a three-part summary (problem / solution / outcome) for long
silent threads using the Navigator model (claude-haiku — cheap, frequent calls).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from llm.provider.base import ChatRequest, Message, build_provider

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a Linux kernel expert summarizing LKML email threads.
Produce a concise three-part summary in JSON:
{"problem": "...", "solution": "...", "outcome": "..."}
- problem: what bug/issue was being discussed
- solution: what fix or approach was proposed or merged
- outcome: was it resolved? any follow-up needed?
Keep each field under 3 sentences. Respond ONLY with the JSON object."""

_MAX_BODY_CHARS = 12000  # truncate long threads to fit Navigator context


class ThreadSummarizer:
    """Summarizes LKML threads using the Navigator LLM role."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self._provider = build_provider(role="navigator")

    def summarize_thread(self, thread_id: int) -> bool:
        """Summarize one thread and write result to PG. Returns True on success."""
        messages = self._load_thread_messages(thread_id)
        if not messages:
            return False

        thread_text = self._format_thread(messages)
        if len(thread_text) > _MAX_BODY_CHARS:
            thread_text = thread_text[:_MAX_BODY_CHARS] + "\n[...truncated...]"

        req = ChatRequest(
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=f"Summarize this LKML thread:\n\n{thread_text}"),
            ],
            temperature=0.0,
            stream=False,
            metadata={"role": "navigator", "purpose": "lkml_summary"},
        )

        try:
            resp = self._provider.chat(req)
            import json
            summary = json.loads(resp.content.strip())
            self._write_summary(thread_id, summary)
            return True
        except Exception as exc:
            logger.warning("[lkml/summarizer] thread %d failed: %s", thread_id, exc)
            self._write_summary(thread_id, {
                "problem": f"[summary-failed: {exc}]",
                "solution": "",
                "outcome": "",
            })
            return False

    def _load_thread_messages(self, thread_id: int) -> list[dict]:
        sql = text("""
            SELECT author_name, author_email, date, subject, body
              FROM lkml_message
             WHERE thread_id = :tid
             ORDER BY date ASC
             LIMIT 50
        """)
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"tid": thread_id}).fetchall()
        return [
            {"author": f"{r[0]} <{r[1]}>", "date": str(r[2]),
             "subject": r[3], "body": (r[4] or "")[:800]}
            for r in rows
        ]

    def _format_thread(self, messages: list[dict]) -> str:
        parts = []
        for m in messages:
            parts.append(f"--- {m['author']} ({m['date']}) ---\n{m['subject']}\n{m['body']}")
        return "\n\n".join(parts)

    def _write_summary(self, thread_id: int, summary: dict) -> None:
        sql = text("""
            UPDATE lkml_thread
               SET summary_problem = :p,
                   summary_solution = :s,
                   summary_outcome = :o,
                   summary_generated_at = :at
             WHERE id = :tid
        """)
        with self._engine.connect() as conn:
            conn.execute(sql, {
                "p": summary.get("problem", ""),
                "s": summary.get("solution", ""),
                "o": summary.get("outcome", ""),
                "at": datetime.now(timezone.utc),
                "tid": thread_id,
            })
            conn.commit()
