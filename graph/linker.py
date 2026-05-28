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
    """Stream kernel_commit.body for bugzilla.kernel.org / bsc#/BZ-# refs and
    link to the bug row identified by (source='bugzilla_kernel', external_id).

    Pre-v2.3 this had two bugs:
      1. WHERE LIMIT :batch_size truncated each invocation to N commits with
         no checkpoint — so only the first N commits ever got processed
         (same trap as link_nvd_commits).
      2. The bug-existence check used `bug.id = :extracted_number`, but
         `bug.id` is the table's auto-increment PK, not the bugzilla number.
         The bugzilla number lives in `bug.external_id`. So 1,717 commits
         citing bugzilla.kernel.org produced just 1 link (the one accidental
         id-collision).
    Result: link_commit_bug had ~1 bugzilla-source row instead of ~1.7K.

    Fix follows the link_gitee_atomgit_bugs pattern (which never had this
    bug): build an in-memory (external_id → bug.id) index, stream all
    candidate commits with a single SQL query, bulk INSERT in CHUNKs.
    `batch_size` is retained for API compat but no longer truncates.
    """
    bug_idx: dict[str, int] = {
        r.external_id: r.id
        for r in conn.execute(text(
            "SELECT id, external_id FROM bug WHERE source = 'bugzilla_kernel'"
        ))
        if r.external_id  # skip NULLs
    }
    logger.info("[linker] bugzilla_kernel index size: %d", len(bug_idx))
    if not bug_idx:
        return

    # Stream all commits whose body mentions a bugzilla.kernel.org URL or a
    # short bsc#/BZ-/bz#/bug# trailer. ILIKE for case-insensitive prefix
    # match — covers 'bugzilla.kernel.org' (URL form) plus the short forms.
    sql = text("""
        SELECT hash, body FROM kernel_commit
         WHERE body IS NOT NULL
           AND (body ILIKE '%bugzilla.kernel.org/show_bug%'
                OR body ~* '(bsc#|BZ-|bz#|bug #?)\\d+')
    """)
    pairs: list[tuple[str, int, str]] = []   # (commit_hash, bug_pk, link_type)
    skipped_no_bug = 0
    # Buffer fully — the WHERE clause limits candidates to ~10K rows max
    # (1.7K bugzilla URL + ~10K bsc# refs). Server-side cursor would
    # conflict with executemany on the same connection.
    for hsh, body in conn.execute(sql).fetchall():
        for bug_id_str, link_type in _extract_bugzilla_ids(body or ""):
            bug_pk = bug_idx.get(bug_id_str)
            if bug_pk is None:
                skipped_no_bug += 1
                continue
            pairs.append((hsh, bug_pk, link_type))
    # Dedup (commit, bug, link_type) tuples
    pairs = list({p: None for p in pairs}.keys())
    logger.info(
        "[linker] bugzilla commit→bug pairs: %d (skipped %d refs with no matching bug row)",
        len(pairs), skipped_no_bug,
    )
    report.commit_bug_skipped += skipped_no_bug

    insert_sql = text("""
        INSERT INTO link_commit_bug
               (commit_hash, bug_id, link_type, confidence, source)
        VALUES (:h, :b, :lt, 0.95, 'trailer')
        ON CONFLICT ON CONSTRAINT uq_lcb DO NOTHING
    """)
    CHUNK = 1000
    batch: list[dict] = []
    for hsh, pk, lt in pairs:
        batch.append({"h": hsh, "b": pk, "lt": lt})
        if len(batch) >= CHUNK:
            conn.execute(insert_sql, batch)
            batch.clear()
    if batch:
        conn.execute(insert_sql, batch)
    report.commit_bug_inserted += len(pairs)


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
    """Stream kernel_commit.body for Link: lore.kernel.org trailers and link
    to lkml_message rows where the message_id is present in our LKML cache.

    Pre-v2.3 had the same LIMIT-truncation trap as _link_trailer_to_bug —
    only `batch_size` commits processed per call, no checkpoint, so the
    function only ever scratched the surface.

    Fix: build an in-memory message_id set (~115K rows, well within memory)
    and stream all candidate commits. Subject-fuzzy fallback is dropped in
    bulk path because it does one ILIKE per commit — quadratic at scale
    and low-confidence anyway. Pure Link: trailer linking is high-precision.
    """
    from ingest.lkml.backfill import _clean_msgid  # noqa: PLC0415

    msg_set: set[str] = {r.message_id for r in conn.execute(text(
        "SELECT message_id FROM lkml_message"
    ))}
    logger.info("[linker] lkml_message index size: %d", len(msg_set))
    if not msg_set:
        return

    sql = text("""
        SELECT hash, body FROM kernel_commit
         WHERE body IS NOT NULL
           AND body ILIKE '%lore.kernel.org%'
    """)
    pairs: list[tuple[str, str]] = []   # (commit_hash, msg_id)
    skipped_msgid_not_in_cache = 0
    # Buffer rather than stream — only ~40K commits cite lore.kernel.org,
    # well within memory. Server-side cursor conflicts with executemany
    # on the same SQLAlchemy connection.
    for hsh, body in conn.execute(sql).fetchall():
        seen_for_this_commit: set[str] = set()
        for m in _LORE_URL_RE.finditer(body or ""):
            msg_id = _clean_msgid(m.group(1))
            if not msg_id or "@" not in msg_id:
                continue
            if msg_id not in msg_set:
                skipped_msgid_not_in_cache += 1
                continue
            if msg_id in seen_for_this_commit:
                continue
            seen_for_this_commit.add(msg_id)
            pairs.append((hsh, msg_id))
    # Dedup global
    pairs = list({p: None for p in pairs}.keys())
    logger.info(
        "[linker] commit→message pairs: %d (skipped %d refs whose msg-id is not in lkml_message cache)",
        len(pairs), skipped_msgid_not_in_cache,
    )

    insert_sql = text("""
        INSERT INTO link_commit_message
               (commit_hash, message_id, link_type, match_method, confidence)
        VALUES (:h, :mid, 'link_trailer', 'link_trailer', 0.9)
        ON CONFLICT ON CONSTRAINT uq_lcm DO NOTHING
    """)
    CHUNK = 1000
    batch: list[dict] = []
    for hsh, mid in pairs:
        batch.append({"h": hsh, "mid": mid})
        if len(batch) >= CHUNK:
            conn.execute(insert_sql, batch)
            batch.clear()
    if batch:
        conn.execute(insert_sql, batch)
    report.commit_message_inserted += len(pairs)


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
    """For OLK commits whose upstream_commit SHA isn't in kernel_commit yet,
    insert a stub mainline row so the cross-graph linker can surface the
    OLK ↔ upstream bridge.

    Pre-v2.3: `LIMIT 500` capped each invocation to 500 new stubs (and the
    per-row Python loop with SAVEPOINTs was 30× slower than a bulk insert).
    Audit found 14,661 distinct orphan upstream SHAs remaining when the
    function had logged "upstream_bridged: 500" repeatedly — it would have
    needed 30+ invocations to finish.

    Fix is one bulk INSERT…SELECT grouped by upstream_commit (dedup) with
    ON CONFLICT (hash) DO NOTHING. The stub-subject format is preserved
    verbatim — `[stub upstream for OLK <hash[:12]>]` — because
    graph/reconcile.py pattern-matches it (see graph/CLAUDE.md).
    """
    before = conn.execute(text("""
        SELECT count(*) FROM kernel_commit WHERE subject LIKE '[stub upstream for OLK %'
    """)).scalar() or 0

    conn.execute(text("""
        INSERT INTO kernel_commit
            (hash, subject, commit_date, origin, affected_versions)
        SELECT olk.upstream_commit,
               '[stub upstream for OLK ' || substring(MIN(olk.hash), 1, 12) || ']',
               '1970-01-01'::timestamptz,
               'mainline',
               ARRAY['mainline']
          FROM kernel_commit olk
          LEFT JOIN kernel_commit main ON main.hash = olk.upstream_commit
         WHERE olk.upstream_commit IS NOT NULL
           AND olk.origin = 'olk'
           AND main.hash IS NULL
         GROUP BY olk.upstream_commit
        ON CONFLICT (hash) DO NOTHING
    """))

    after = conn.execute(text("""
        SELECT count(*) FROM kernel_commit WHERE subject LIKE '[stub upstream for OLK %'
    """)).scalar() or 0

    inserted = after - before
    report.upstream_bridged += inserted
    logger.info("[linker] upstream bridge stubs: %d → %d (+%d)",
                before, after, inserted)


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
