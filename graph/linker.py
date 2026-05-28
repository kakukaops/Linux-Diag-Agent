"""Cross-Graph Linker (WBS 3.2 / ADR-018).

Writes link_commit_bug, link_commit_cve rows in Postgres by mining:
  1. Commit trailers (Fixes:/Closes:/Link:) → bug / LKML message
  2. OLK ↔ upstream SHA bridging via upstream_commit column
  3. NVD fix-commit references → link_commit_cve (WBS 3.3)

Run after each kernel_commit or nvd ingestion cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Bugzilla URL → numeric ID
_BZ_URL_RE = re.compile(r"bugzilla\.kernel\.org/show_bug\.cgi\?id=(\d+)")
# Short bugzilla ref in trailer text: "bsc#12345", "BZ-12345", "#12345"
_BZ_SHORT_RE = re.compile(r"(?:bsc#|BZ-|bz#|bug #?)(\d+)", re.IGNORECASE)
# CVE pattern in trailers
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
# Link: trailer pointing to lore message-ID URL
_LORE_URL_RE = re.compile(
    r"https?://lore\.kernel\.org/[^\s]+/([^/\s>]+@[^/\s>]+)",
    re.IGNORECASE,
)
# Subject-based match prefix (strip Re:/Patch prefixes)
_SUBJECT_CLEAN_RE = re.compile(r"^\s*(?:Re|Patch|RFC|PATCH)[:/\s]*", re.IGNORECASE)


@dataclass
class LinkReport:
    commit_bug_inserted: int = 0
    commit_bug_skipped: int = 0
    commit_cve_inserted: int = 0
    commit_message_inserted: int = 0
    upstream_bridged: int = 0
    errors: list[str] = field(default_factory=list)


# ── Public entry point ────────────────────────────────────────────────────────


def run_linker(engine: "Engine", *, batch_size: int = 1000) -> LinkReport:
    """Mine all unlinked commits and write link rows.  Returns a summary report."""
    report = LinkReport()

    with engine.begin() as conn:
        _link_trailer_to_bug(conn, batch_size, report)
        _link_link_trailer_to_message(conn, batch_size, report)
        _link_olk_upstream(conn, report)

    logger.info(
        "Linker done: %d commit-bug, %d commit-msg, %d upstream bridges, %d errors",
        report.commit_bug_inserted,
        report.commit_message_inserted,
        report.upstream_bridged,
        len(report.errors),
    )
    return report


# ── Trailer → Bug linker ──────────────────────────────────────────────────────


def _link_trailer_to_bug(conn: Any, batch_size: int, report: LinkReport) -> None:
    """Scan kernel_commit.body for Fixes:/Closes: refs that match bug table rows."""
    sql = text("""
        SELECT kc.hash, kc.body
          FROM kernel_commit kc
         WHERE kc.body IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM link_commit_bug lcb
                WHERE lcb.commit_hash = kc.hash
                  AND lcb.source = 'trailer'
           )
         LIMIT :limit
    """)
    rows = conn.execute(sql, {"limit": batch_size}).fetchall()

    for commit_hash, body in rows:
        bug_ids = _extract_bugzilla_ids(body or "")
        cve_ids = _CVE_RE.findall(body or "")

        for bug_id_str, link_type in bug_ids:
            try:
                bug_id = int(bug_id_str)
                exists = conn.execute(
                    text("SELECT 1 FROM bug WHERE id = :id"), {"id": bug_id}
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    text("""
                        INSERT INTO link_commit_bug
                               (commit_hash, bug_id, link_type, confidence, source)
                        VALUES (:h, :b, :lt, 0.95, 'trailer')
                        ON CONFLICT ON CONSTRAINT uq_lcb DO NOTHING
                    """),
                    {"h": commit_hash, "b": bug_id, "lt": link_type},
                )
                report.commit_bug_inserted += 1
            except Exception as exc:
                report.errors.append(f"commit {commit_hash} bug {bug_id_str}: {exc}")
                report.commit_bug_skipped += 1


def _extract_bugzilla_ids(body: str) -> list[tuple[str, str]]:
    """Return [(bug_id_str, link_type)] from commit body."""
    results: list[tuple[str, str]] = []
    for m in _BZ_URL_RE.finditer(body):
        results.append((m.group(1), "closes"))
    for m in _BZ_SHORT_RE.finditer(body):
        results.append((m.group(1), "fixes"))
    return results


# ── Link: trailer → LKML message linker (WBS 4.2) ────────────────────────────


def _link_link_trailer_to_message(conn: Any, batch_size: int, report: LinkReport) -> None:
    """Mine Link:/In-reply-to: trailers to create link_commit_message rows."""
    sql = text("""
        SELECT kc.hash, kc.body, kc.subject
          FROM kernel_commit kc
         WHERE kc.body IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM link_commit_message lcm
                WHERE lcm.commit_hash = kc.hash
                  AND lcm.match_method IN ('link_trailer', 'subject')
           )
         LIMIT :limit
    """)
    rows = conn.execute(sql, {"limit": batch_size}).fetchall()

    for commit_hash, body, subject in rows:
        body = body or ""
        # 1. Lore URL → message_id from Link: trailers (strip trailing
        # punctuation captured by the regex — see ingest/lkml/backfill._clean_msgid)
        from ingest.lkml.backfill import _clean_msgid  # noqa: PLC0415
        for m in _LORE_URL_RE.finditer(body):
            msg_id = _clean_msgid(m.group(1))
            if not msg_id or "@" not in msg_id:
                continue
            _upsert_commit_message_link(conn, commit_hash, msg_id, "link_trailer", report)

        # 2. Subject match fallback — search lkml_message by cleaned subject
        if subject:
            clean_subject = _SUBJECT_CLEAN_RE.sub("", subject).strip()
            if clean_subject:
                row = conn.execute(
                    text("""
                        SELECT message_id FROM lkml_message
                         WHERE subject ILIKE :s
                         LIMIT 1
                    """),
                    {"s": f"%{clean_subject[:80]}%"},
                ).fetchone()
                if row:
                    _upsert_commit_message_link(conn, commit_hash, row[0], "subject", report)


def _upsert_commit_message_link(
    conn: Any,
    commit_hash: str,
    message_id: str,
    method: str,
    report: LinkReport,
) -> None:
    try:
        exists = conn.execute(
            text("SELECT 1 FROM lkml_message WHERE message_id = :mid"),
            {"mid": message_id},
        ).fetchone()
        if not exists:
            return
        conn.execute(
            text("""
                INSERT INTO link_commit_message
                       (commit_hash, message_id, link_type, match_method, confidence)
                VALUES (:h, :mid, 'link_trailer', :method, 0.9)
                ON CONFLICT ON CONSTRAINT uq_lcm DO NOTHING
            """),
            {"h": commit_hash, "mid": message_id, "method": method},
        )
        report.commit_message_inserted += 1
    except Exception as exc:
        report.errors.append(f"commit_message link {commit_hash}/{message_id}: {exc}")


# ── OLK ↔ upstream bridge ─────────────────────────────────────────────────────


def _link_olk_upstream(conn: Any, report: LinkReport) -> None:
    """Ensure mainline SHA exists in kernel_commit; update affected_versions."""
    sql = text("""
        SELECT olk.hash AS olk_hash,
               olk.upstream_commit AS upstream_sha,
               main.hash AS main_hash
          FROM kernel_commit olk
          LEFT JOIN kernel_commit main ON main.hash = olk.upstream_commit
         WHERE olk.upstream_commit IS NOT NULL
           AND olk.origin = 'olk'
           AND main.hash IS NULL
         LIMIT 500
    """)
    rows = conn.execute(sql).fetchall()
    for olk_hash, upstream_sha, _ in rows:
        # The upstream commit isn't in our DB yet — record a stub so the graph
        # linker can surface the bridge.  commit_date uses epoch as sentinel.
        try:
            conn.execute(text("SAVEPOINT stub_ins"))
            conn.execute(
                text("""
                    INSERT INTO kernel_commit
                        (hash, subject, commit_date, origin, affected_versions)
                    VALUES (:h, :s, '1970-01-01'::timestamptz, 'mainline', ARRAY['mainline'])
                    ON CONFLICT (hash) DO NOTHING
                """),
                {"h": upstream_sha, "s": f"[stub upstream for OLK {olk_hash[:12]}]"},
            )
            conn.execute(text("RELEASE SAVEPOINT stub_ins"))
            report.upstream_bridged += 1
        except Exception as exc:
            conn.execute(text("ROLLBACK TO SAVEPOINT stub_ins"))
            report.errors.append(f"upstream bridge {upstream_sha}: {exc}")


# ── NVD → commit linker (WBS 3.3) ────────────────────────────────────────────


def link_nvd_commits(engine: "Engine", *, batch_size: int | None = None) -> int:
    """Cross-reference NVD fix_commits JSON against kernel_commit hashes.

    Walks ALL CVE rows that have fix_commits populated and inserts (commit,
    cve) edges for every SHA that prefix-matches a kernel_commit row. Uses
    a single bulk INSERT…SELECT via LATERAL json_array_elements_text so the
    full link table is rebuilt in one DB round-trip.

    The optional `batch_size` argument is kept for API compatibility but no
    longer truncates the result set — prior to v2.3 it silently capped the
    function at 500 CVEs (≈ 547 rows out of a 2,870-row upper bound), which
    is why link_commit_cve was 5× too small.

    Returns count of new link_commit_cve rows inserted.
    """
    if batch_size is not None and batch_size < 100:
        logger.warning(
            "link_nvd_commits batch_size %d ignored; processing all CVEs.",
            batch_size,
        )

    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT count(*) FROM link_commit_cve")
        ).scalar() or 0

        # One-shot bulk insert. JSON-array elements are exploded LATERAL,
        # prefix-matched against kernel_commit via LIKE on the 12-char head.
        # Deduplication is handled by uq_lcc + ON CONFLICT DO NOTHING.
        conn.execute(text("""
            INSERT INTO link_commit_cve (commit_hash, cve_id, link_type, source)
            SELECT DISTINCT kc.hash, c.cve_id, 'nvd_ref', 'nvd'
              FROM cve c
              CROSS JOIN LATERAL json_array_elements_text(c.fix_commits) AS sha
              JOIN kernel_commit kc
                ON kc.hash LIKE substring(lower(sha) FROM 1 FOR 12) || '%'
             WHERE c.fix_commits IS NOT NULL
               AND json_array_length(c.fix_commits) > 0
            ON CONFLICT ON CONSTRAINT uq_lcc DO NOTHING
        """))

        after = conn.execute(
            text("SELECT count(*) FROM link_commit_cve")
        ).scalar() or 0

    inserted = after - before
    logger.info("NVD linker inserted %d commit-cve rows (table: %d → %d)",
                inserted, before, after)
    return inserted


# ── gitee / atomgit issue linker (ADR-022) ────────────────────────────────────

_GITEE_ISSUE_RE = re.compile(
    r"gitee\.com/openeuler/kernel/issues/([A-Z0-9]+)", re.IGNORECASE,
)
_ATOMGIT_ISSUE_RE = re.compile(
    r"atomgit\.com/openeuler/kernel/issues/(\d+)", re.IGNORECASE,
)


def link_gitee_atomgit_bugs(engine: "Engine") -> dict[str, int]:
    """Wire commit↔bug links for gitee + atomgit issue references (ADR-022).

    For each commit citing a gitee/atomgit issue URL in its body, look up the
    matching bug row by (source, external_id) and INSERT into link_commit_bug.

    Returns counts per source.
    """
    inserted = {"gitee": 0, "atomgit": 0}
    skipped_no_bug = {"gitee": 0, "atomgit": 0}

    # Build (source, external_id) -> bug.id index once. ~4K rows for gitee
    # after backfill — well within memory.
    with engine.connect() as conn:
        bug_idx: dict[tuple[str, str], int] = {
            (r.source, r.external_id.upper()): r.id
            for r in conn.execute(
                text("SELECT id, source, external_id FROM bug "
                     "WHERE source IN ('gitee','atomgit')")
            )
        }
    logger.info("[linker] gitee/atomgit bug index size: %d", len(bug_idx))
    if not bug_idx:
        return inserted

    # Stream all commits that cite either platform.
    sql = text("""
        SELECT hash, body FROM kernel_commit
         WHERE body ILIKE '%gitee.com/openeuler/kernel/issues%'
            OR body ILIKE '%atomgit.com/openeuler/kernel/issues%'
    """)
    pairs: list[tuple[str, str, str, str]] = []  # (commit_hash, source, ext_id, link_type)
    with engine.connect() as conn:
        cur = conn.execution_options(stream_results=True).execute(sql)
        for hsh, body in cur:
            for m in _GITEE_ISSUE_RE.finditer(body or ""):
                pairs.append((hsh, "gitee", m.group(1).upper(), "trailer"))
            for m in _ATOMGIT_ISSUE_RE.finditer(body or ""):
                pairs.append((hsh, "atomgit", m.group(1), "trailer"))
    # Dedup
    pairs = list({p: None for p in pairs}.keys())
    logger.info("[linker] candidate commit→bug pairs: %d", len(pairs))

    insert_sql = text("""
        INSERT INTO link_commit_bug (commit_hash, bug_id, link_type, confidence, source)
        VALUES (:h, :b, :lt, 0.95, :src)
        ON CONFLICT ON CONSTRAINT uq_lcb DO NOTHING
    """)
    CHUNK = 1000
    with engine.begin() as conn:
        batch = []
        for hsh, source, ext_id, lt in pairs:
            bug_id = bug_idx.get((source, ext_id))
            if bug_id is None:
                skipped_no_bug[source] += 1
                continue
            batch.append({"h": hsh, "b": bug_id, "lt": lt, "src": source})
            inserted[source] += 1
            if len(batch) >= CHUNK:
                conn.execute(insert_sql, batch)
                batch.clear()
        if batch:
            conn.execute(insert_sql, batch)

    logger.info("[linker] inserted gitee=%d atomgit=%d (skipped no-bug: gitee=%d atomgit=%d)",
                inserted["gitee"], inserted["atomgit"],
                skipped_no_bug["gitee"], skipped_no_bug["atomgit"])
    return inserted
