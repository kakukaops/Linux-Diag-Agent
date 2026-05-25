"""LangGraph PG checkpointer (WBS 7.12).

Uses langgraph-checkpoint-postgres (PostgresSaver) so diagnosis runs are
resumable after crashes or rate-limit interruptions.

The custom PgCheckpointer class is kept for reference / low-level access but
the LangGraph-compatible saver is what build_graph() actually wires.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_saver = None


def get_checkpointer():
    """Return a langgraph-compatible PostgresSaver singleton.

    Creates the checkpoint tables on first call (idempotent).
    Falls back to InMemorySaver if PG is unavailable.
    """
    global _saver
    if _saver is not None:
        return _saver

    from configs.config import get_config
    cfg = get_config()
    # Strip SQLAlchemy driver prefix → plain psycopg3 DSN
    dsn = cfg.database.postgres.dsn.replace("postgresql+psycopg://", "postgresql://")

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
        # autocommit required for CREATE INDEX CONCURRENTLY in setup()
        conn = psycopg.connect(dsn, autocommit=True)
        saver = PostgresSaver(conn)
        saver.setup()
        _saver = saver
        logger.info("PgCheckpointer: using PostgresSaver on %s", dsn.split("@")[-1])
    except Exception as exc:
        logger.warning("PgCheckpointer: PostgresSaver failed (%s), falling back to InMemorySaver", exc)
        from langgraph.checkpoint.memory import InMemorySaver
        _saver = InMemorySaver()

    return _saver
