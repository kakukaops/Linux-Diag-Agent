"""Neo4j weekly full rebuild (WBS 4.3).

Exports the PG link tables into Neo4j as a property graph.

Graph model:
  (:Commit {hash, subject, origin, olk_inclusion_type})
  (:Bug    {id, summary, status})
  (:CVE    {cve_id, severity, cvss_score})
  (:Message {message_id, subject})

Relationships:
  (Commit)-[:FIXES]->(Bug)           from link_commit_bug.link_type='fixes'
  (Commit)-[:CLOSES]->(Bug)          from link_commit_bug.link_type='closes'
  (Commit)-[:FIXES_CVE]->(CVE)       from link_commit_cve
  (Commit)-[:LINKED_TO]->(Message)   from link_commit_message
  (Commit)-[:BACKPORTS]->(Commit)    upstream_commit reference
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import text

logger = logging.getLogger(__name__)

_BATCH = 500


def rebuild(pg_engine: Any, neo4j_uri: str, neo4j_auth: tuple[str, str]) -> dict[str, int]:
    """Drop and rebuild all Neo4j nodes/relationships from PG.

    Returns counts of created nodes and relationships.
    """
    driver = GraphDatabase.driver(neo4j_uri, auth=neo4j_auth)
    counts: dict[str, int] = {}
    try:
        with driver.session() as session:
            _clear_graph(session)
            counts["commit_nodes"] = _load_commits(session, pg_engine)
            counts["bug_nodes"] = _load_bugs(session, pg_engine)
            counts["cve_nodes"] = _load_cves(session, pg_engine)
            counts["message_nodes"] = _load_messages(session, pg_engine)
            counts["commit_bug_rels"] = _load_commit_bug_rels(session, pg_engine)
            counts["commit_cve_rels"] = _load_commit_cve_rels(session, pg_engine)
            counts["commit_msg_rels"] = _load_commit_msg_rels(session, pg_engine)
            counts["backport_rels"] = _load_backport_rels(session, pg_engine)
    finally:
        driver.close()

    logger.info("Neo4j rebuild complete: %s", counts)
    return counts


# ── Graph wipe ────────────────────────────────────────────────────────────────


def _clear_graph(session: Any) -> None:
    session.run("MATCH (n) DETACH DELETE n")
    logger.info("Neo4j graph cleared")


# ── Node loaders ──────────────────────────────────────────────────────────────


def _load_commits(session: Any, engine: Any) -> int:
    sql = text("""
        SELECT hash, subject, origin, olk_inclusion_type
          FROM kernel_commit
         WHERE subject NOT LIKE '[stub upstream%'
    """)
    return _batch_merge(
        session, engine, sql, [],
        "MERGE (c:Commit {hash: row.hash}) "
        "SET c.subject = row.subject, c.origin = row.origin, "
        "    c.olk_inclusion_type = row.olk_inclusion_type",
    )


def _load_bugs(session: Any, engine: Any) -> int:
    sql = text("SELECT id, summary, status FROM bug")
    return _batch_merge(
        session, engine, sql, [],
        "MERGE (b:Bug {id: row.id}) SET b.summary = row.summary, b.status = row.status",
    )


def _load_cves(session: Any, engine: Any) -> int:
    sql = text("SELECT cve_id, severity, cvss_score FROM cve")
    return _batch_merge(
        session, engine, sql, [],
        "MERGE (v:CVE {cve_id: row.cve_id}) "
        "SET v.severity = row.severity, v.cvss_score = toFloat(toString(row.cvss_score))",
    )


def _load_messages(session: Any, engine: Any) -> int:
    sql = text("SELECT message_id, subject FROM lkml_message LIMIT 200000")
    return _batch_merge(
        session, engine, sql, [],
        "MERGE (m:Message {message_id: row.message_id}) SET m.subject = row.subject",
    )


# ── Relationship loaders ──────────────────────────────────────────────────────


def _load_commit_bug_rels(session: Any, engine: Any) -> int:
    sql = text("""
        SELECT commit_hash, bug_id, link_type
          FROM link_commit_bug
    """)
    count = 0
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    for i in range(0, len(rows), _BATCH):
        batch = [{"h": r[0], "b": r[1], "t": r[2]} for r in rows[i:i+_BATCH]]
        rel_type = "FIXES"
        session.run(
            f"UNWIND $batch AS row "
            f"MATCH (c:Commit {{hash: row.h}}), (b:Bug {{id: row.b}}) "
            f"MERGE (c)-[:{rel_type} {{link_type: row.t}}]->(b)",
            batch=batch,
        )
        count += len(batch)
    return count


def _load_commit_cve_rels(session: Any, engine: Any) -> int:
    sql = text("SELECT commit_hash, cve_id FROM link_commit_cve")
    count = 0
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    for i in range(0, len(rows), _BATCH):
        batch = [{"h": r[0], "c": r[1]} for r in rows[i:i+_BATCH]]
        session.run(
            "UNWIND $batch AS row "
            "MATCH (c:Commit {hash: row.h}), (v:CVE {cve_id: row.c}) "
            "MERGE (c)-[:FIXES_CVE]->(v)",
            batch=batch,
        )
        count += len(batch)
    return count


def _load_commit_msg_rels(session: Any, engine: Any) -> int:
    sql = text("SELECT commit_hash, message_id FROM link_commit_message")
    count = 0
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    for i in range(0, len(rows), _BATCH):
        batch = [{"h": r[0], "m": r[1]} for r in rows[i:i+_BATCH]]
        session.run(
            "UNWIND $batch AS row "
            "MATCH (c:Commit {hash: row.h}), (msg:Message {message_id: row.m}) "
            "MERGE (c)-[:LINKED_TO]->(msg)",
            batch=batch,
        )
        count += len(batch)
    return count


def _load_backport_rels(session: Any, engine: Any) -> int:
    sql = text("""
        SELECT hash, upstream_commit
          FROM kernel_commit
         WHERE upstream_commit IS NOT NULL
           AND origin = 'olk'
    """)
    count = 0
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    for i in range(0, len(rows), _BATCH):
        batch = [{"olk": r[0], "up": r[1]} for r in rows[i:i+_BATCH]]
        session.run(
            "UNWIND $batch AS row "
            "MATCH (o:Commit {hash: row.olk}), (u:Commit {hash: row.up}) "
            "MERGE (o)-[:BACKPORTS]->(u)",
            batch=batch,
        )
        count += len(batch)
    return count


# ── Batch helper ──────────────────────────────────────────────────────────────


def _batch_merge(
    session: Any,
    engine: Any,
    sql: Any,
    params: list,
    cypher_merge: str,
) -> int:
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
        keys = conn.execute(sql).keys() if not rows else None

    if not rows:
        return 0

    col_names = list(rows[0]._mapping.keys())
    count = 0
    for i in range(0, len(rows), _BATCH):
        batch = [dict(zip(col_names, r)) for r in rows[i:i+_BATCH]]
        session.run(f"UNWIND $batch AS row {cypher_merge}", batch=batch)
        count += len(batch)
    return count
