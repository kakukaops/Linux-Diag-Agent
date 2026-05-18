"""LangGraph PG checkpointer (WBS 7.12).

Saves and restores LangGraph state to/from the Postgres `agent_checkpoints`
table so diagnosis runs are resumable after crashes or rate-limit interruptions.

Uses langgraph's SqliteSaver interface as a template; stores JSON in PG instead.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import text

logger = logging.getLogger(__name__)


class PgCheckpointer:
    """Simple key-value checkpoint store backed by Postgres."""

    TABLE = "agent_checkpoints"

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    thread_id   TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL DEFAULT gen_random_uuid()::text,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    state       JSONB NOT NULL,
                    metadata    JSONB,
                    PRIMARY KEY (thread_id, checkpoint_id)
                )
            """))
            conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_ac_thread
                  ON {self.TABLE}(thread_id, created_at DESC)
            """))

    # ── LangGraph-compatible interface ────────────────────────────────────────

    def put(self, config: dict[str, Any], state: dict[str, Any], metadata: dict | None = None) -> str:
        """Persist a checkpoint. Returns the checkpoint_id."""
        thread_id = config.get("configurable", {}).get("thread_id", str(uuid4()))
        checkpoint_id = str(uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                text(f"""
                    INSERT INTO {self.TABLE} (thread_id, checkpoint_id, state, metadata)
                    VALUES (:tid, :cid, :state, :meta)
                """),
                {
                    "tid": thread_id,
                    "cid": checkpoint_id,
                    "state": json.dumps(state, default=str),
                    "meta": json.dumps(metadata or {}, default=str),
                },
            )
        return checkpoint_id

    def get(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """Return latest checkpoint for a thread, or None."""
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"""
                    SELECT state FROM {self.TABLE}
                     WHERE thread_id = :tid
                     ORDER BY created_at DESC
                     LIMIT 1
                """),
                {"tid": thread_id},
            ).fetchone()
        if row:
            return json.loads(row[0])
        return None

    def list(self, config: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent checkpoints for a thread."""
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT checkpoint_id, created_at, state, metadata
                      FROM {self.TABLE}
                     WHERE thread_id = :tid
                     ORDER BY created_at DESC
                     LIMIT :lim
                """),
                {"tid": thread_id, "lim": limit},
            ).fetchall()
        return [
            {
                "checkpoint_id": r[0],
                "created_at": str(r[1]),
                "state": json.loads(r[2]),
                "metadata": json.loads(r[3]) if r[3] else {},
            }
            for r in rows
        ]

    def delete(self, thread_id: str) -> None:
        """Delete all checkpoints for a thread."""
        with self._engine.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {self.TABLE} WHERE thread_id = :tid"),
                {"tid": thread_id},
            )


_checkpointer: PgCheckpointer | None = None


def get_checkpointer() -> PgCheckpointer:
    global _checkpointer
    if _checkpointer is None:
        from storage.pg.engine import get_engine
        _checkpointer = PgCheckpointer(get_engine())
    return _checkpointer
