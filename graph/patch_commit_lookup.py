"""Patch-commit reverse lookup (WBS 4.1).

Given an LKML patch message, find the kernel commit that applied it using:
  1. git patch-id (canonical diff fingerprint, most reliable)
  2. Subject-based fuzzy match (fallback)

Used by the Cross-Graph Linker to create link_commit_message rows.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

_DIFF_STAT_RE = re.compile(r"^diff --git ", re.MULTILINE)
_PATCH_ID_CMD = ["git", "patch-id", "--stable"]
_SUBJECT_PREFIXES_RE = re.compile(
    r"^\s*(?:\[(?:PATCH|RFC)[^\]]*\]|\d+/\d+|Re:)\s*", re.IGNORECASE
)


@dataclass
class PatchMatch:
    message_id: str
    commit_hash: str
    match_method: str   # 'patch_id' | 'subject'
    confidence: float


def find_commit_for_patch(
    message_id: str,
    patch_body: str,
    subject: str,
    repo_path: str | Path,
) -> PatchMatch | None:
    """Return the best commit match for a patch message, or None."""
    # 1. git patch-id
    if _DIFF_STAT_RE.search(patch_body):
        match = _patch_id_match(message_id, patch_body, Path(repo_path))
        if match:
            return match

    # 2. Subject fallback (handled by the DB-side linker)
    return None


def compute_patch_id(diff_text: str, repo_path: Path) -> str | None:
    """Run git patch-id on a diff text and return the patch-id hex string."""
    try:
        proc = subprocess.run(
            _PATCH_ID_CMD,
            input=diff_text.encode(),
            capture_output=True,
            cwd=str(repo_path),
            timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        # Output: "<patch-id> <commit-sha>\n"
        return proc.stdout.decode().split()[0]
    except Exception as exc:
        logger.debug("git patch-id failed: %s", exc)
        return None


def _patch_id_match(
    message_id: str,
    patch_body: str,
    repo_path: Path,
) -> PatchMatch | None:
    patch_id = compute_patch_id(patch_body, repo_path)
    if not patch_id:
        return None

    # Look for a commit with the same patch-id stored in kernel_commit.patch_id
    from storage.pg.engine import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT hash FROM kernel_commit WHERE patch_id = :pid LIMIT 1"),
            {"pid": patch_id},
        ).fetchone()
    if row:
        return PatchMatch(
            message_id=message_id,
            commit_hash=row[0],
            match_method="patch_id",
            confidence=1.0,
        )
    return None


# ── Batch runner ──────────────────────────────────────────────────────────────


def run_patch_lookup(engine: Any, repo_paths: list[str], *, batch_size: int = 200) -> int:
    """Scan unlinked lkml patches and attempt patch-id matching.

    Returns number of new link_commit_message rows inserted.
    """
    inserted = 0
    sql = text("""
        SELECT lm.message_id, lm.subject, lm.body
          FROM lkml_message lm
         WHERE lm.is_patch = TRUE
           AND NOT EXISTS (
               SELECT 1 FROM link_commit_message lcm
                WHERE lcm.message_id = lm.message_id
                  AND lcm.match_method = 'patch_id'
           )
         LIMIT :limit
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"limit": batch_size}).fetchall()

    for msg_id, subject, body in rows:
        for repo_path in repo_paths:
            match = find_commit_for_patch(msg_id, body or "", subject or "", repo_path)
            if match:
                with engine.begin() as conn:
                    conn.execute(
                        text("""
                            INSERT INTO link_commit_message
                                   (commit_hash, message_id, link_type, match_method, confidence)
                            VALUES (:h, :mid, 'patch', 'patch_id', 1.0)
                            ON CONFLICT ON CONSTRAINT uq_lcm DO NOTHING
                        """),
                        {"h": match.commit_hash, "mid": msg_id},
                    )
                inserted += 1
                break  # found in one repo, skip others

    logger.info("Patch lookup: %d new commit-message links", inserted)
    return inserted
