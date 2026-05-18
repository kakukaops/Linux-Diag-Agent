"""Neo4j ↔ PG reconciliation (WBS 4.4).

Compares node/relationship counts between Neo4j and the PG source tables.
Logs WARN if drift exceeds threshold; exits non-zero if critical drift.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import text

logger = logging.getLogger(__name__)

DRIFT_WARN_PCT = 5.0    # warn if node count differs by >5%
DRIFT_ABORT_PCT = 20.0  # fail if node count differs by >20%


def run_reconcile(pg_engine: Any, neo4j_uri: str, neo4j_auth: tuple[str, str]) -> bool:
    """Return True if reconciliation passes (drift within tolerance)."""
    driver = GraphDatabase.driver(neo4j_uri, auth=neo4j_auth)
    ok = True
    try:
        with driver.session() as session:
            ok = _check_commits(session, pg_engine) and ok
            ok = _check_bugs(session, pg_engine) and ok
            ok = _check_cves(session, pg_engine) and ok
    finally:
        driver.close()
    return ok


def _check_commits(session: Any, engine: Any) -> bool:
    neo_count = session.run("MATCH (c:Commit) RETURN count(c) AS n").single()["n"]
    with engine.connect() as conn:
        pg_count = conn.execute(text("SELECT count(*) FROM kernel_commit")).scalar()
    return _check("Commit", neo_count, pg_count)


def _check_bugs(session: Any, engine: Any) -> bool:
    neo_count = session.run("MATCH (b:Bug) RETURN count(b) AS n").single()["n"]
    with engine.connect() as conn:
        pg_count = conn.execute(text("SELECT count(*) FROM bug")).scalar()
    return _check("Bug", neo_count, pg_count)


def _check_cves(session: Any, engine: Any) -> bool:
    neo_count = session.run("MATCH (v:CVE) RETURN count(v) AS n").single()["n"]
    with engine.connect() as conn:
        pg_count = conn.execute(text("SELECT count(*) FROM cve")).scalar()
    return _check("CVE", neo_count, pg_count)


def _check(label: str, neo: int, pg: int) -> bool:
    if pg == 0:
        logger.warning("PG %s count is 0 — skipping drift check", label)
        return True
    drift_pct = abs(neo - pg) / pg * 100
    if drift_pct >= DRIFT_ABORT_PCT:
        logger.error(
            "CRITICAL drift for %s: Neo4j=%d PG=%d (%.1f%%)", label, neo, pg, drift_pct
        )
        return False
    if drift_pct >= DRIFT_WARN_PCT:
        logger.warning(
            "WARN drift for %s: Neo4j=%d PG=%d (%.1f%%)", label, neo, pg, drift_pct
        )
    else:
        logger.info("%s OK: Neo4j=%d PG=%d", label, neo, pg)
    return True


# ── CLI entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from storage.pg.engine import get_engine
    from configs.config import get_config

    cfg = get_config()
    engine = get_engine()
    uri = cfg.database.neo4j.uri
    auth = (cfg.database.neo4j.user, cfg.database.neo4j.password)
    passed = run_reconcile(engine, uri, auth)
    sys.exit(0 if passed else 1)
