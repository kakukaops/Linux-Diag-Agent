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
        # 1. Lore URL → message_id from Link: trailers
        for m in _LORE_URL_RE.finditer(body):
            msg_id = m.group(1)
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
        # The upstream commit isn't in our DB yet — that's fine (linux-stable pull may lag).
        # Record a stub so the graph linker can surface the bridge.
        try:
            conn.execute(
                text("""
                    INSERT INTO kernel_commit (hash, subject, origin, affected_versions)
                    VALUES (:h, :s, 'mainline', ARRAY['mainline'])
                    ON CONFLICT (hash) DO NOTHING
                """),
                {"h": upstream_sha, "s": f"[stub upstream for OLK {olk_hash[:12]}]"},
            )
            report.upstream_bridged += 1
        except Exception as exc:
            report.errors.append(f"upstream bridge {upstream_sha}: {exc}")


# ── NVD → commit linker (WBS 3.3) ────────────────────────────────────────────


def link_nvd_commits(engine: "Engine", *, batch_size: int = 500) -> int:
    """Cross-reference NVD fix_commits JSON against kernel_commit hashes.

    Returns count of new link_commit_cve rows inserted.
    """
    from storage.pg.models import LinkCommitCve  # local import to avoid circular

    inserted = 0
    sql = text("""
        SELECT cve_id, fix_commits
          FROM cve
         WHERE fix_commits IS NOT NULL
           AND jsonb_array_length(fix_commits) > 0
         LIMIT :limit
    """)
    with engine.begin() as conn:
        rows = conn.execute(sql, {"limit": batch_size}).fetchall()
        for cve_id, fix_commits in rows:
            commits: list[str] = fix_commits if isinstance(fix_commits, list) else []
            for sha in commits:
                sha = sha.lower().strip()
                exists = conn.execute(
                    text("SELECT 1 FROM kernel_commit WHERE hash LIKE :prefix"),
                    {"prefix": sha[:12] + "%"},
                ).fetchone()
                if not exists:
                    continue
                full_hash_row = conn.execute(
                    text("SELECT hash FROM kernel_commit WHERE hash LIKE :prefix LIMIT 1"),
                    {"prefix": sha[:12] + "%"},
                ).fetchone()
                if not full_hash_row:
                    continue
                full_hash = full_hash_row[0]
                conn.execute(
                    text("""
                        INSERT INTO link_commit_cve (commit_hash, cve_id, link_type, source)
                        VALUES (:h, :c, 'nvd_ref', 'nvd')
                        ON CONFLICT ON CONSTRAINT uq_lcc DO NOTHING
                    """),
                    {"h": full_hash, "c": cve_id},
                )
                inserted += 1

    logger.info("NVD linker inserted %d commit-cve rows", inserted)
    return inserted
