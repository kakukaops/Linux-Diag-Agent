"""Commit diff retrieval — `git show` over the local OLK kernel repos (v2 T-009).

Returns the actual patch (the code change) of a commit — what a commit message
alone does not convey. Read-only.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 20000   # cap — a commit touching many files can be huge
_GIT_TIMEOUT_S = 30


def get_commit_diff(commit_hash: str, max_chars: int = _MAX_DIFF_CHARS) -> dict[str, Any]:
    """Return the patch of *commit_hash* via `git show` on a local OLK repo.

    Tries the OLK repo for the commit's affected version first, then the other.

    Returns {commit_hash, found: bool, repo, diff, truncated}.
    """
    h = (commit_hash or "").strip()
    if not h:
        return {"commit_hash": commit_hash, "found": False,
                "error": "empty commit_hash"}

    repos = _olk_repos()
    if not repos:
        return {"commit_hash": h, "found": False,
                "error": "no OLK repos configured"}

    # try the commit's own OLK version first (avoids a wasted git call)
    preferred = _preferred_repo_name(h)
    ordered = sorted(repos.items(), key=lambda kv: kv[0] != preferred)

    for name, path in ordered:
        diff = _git_show(path, h)
        if diff is not None:
            return {
                "commit_hash": h,
                "found": True,
                "repo": name,
                "diff": diff[:max_chars],
                "truncated": len(diff) > max_chars,
            }
    return {"commit_hash": h, "found": False,
            "error": "commit not found in any OLK repo"}


def _git_show(repo_path: str, commit_hash: str) -> str | None:
    """`git show <hash>` in *repo_path*; None if the commit is not there."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "show", "--no-color", commit_hash],
            capture_output=True, timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("git show failed in %s: %s", repo_path, exc)
        return None
    if result.returncode != 0:
        return None  # 'fatal: bad object' — commit not in this repo
    # OLK commit messages can contain non-UTF-8 bytes
    return result.stdout.decode("utf-8", errors="replace")


def _olk_repos() -> dict[str, str]:
    """{olk_version_name: local_repo_path} from config."""
    from configs.config import get_config
    return {
        r.name: r.local_path
        for r in get_config().ingestion.kernel_commit.olk_repos
        if r.local_path
    }


def _preferred_repo_name(commit_hash: str) -> str | None:
    """The OLK version a commit belongs to (kernel_commit.affected_versions)."""
    from sqlalchemy import text
    from storage.pg.engine import get_engine
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT affected_versions FROM kernel_commit "
                     "WHERE hash LIKE :p LIMIT 1"),
                {"p": commit_hash + "%"},
            ).fetchone()
    except Exception:
        return None
    return row[0][0] if row and row[0] else None
