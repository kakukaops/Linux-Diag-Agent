"""Git subprocess wrapper for kernel commit ingestion.

Uses subprocess (not GitPython) for reliable --format and --raw parsing.
Processes commits in batches for memory efficiency.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_GIT_LOG_FORMAT = "%x00".join([
    "%H",    # 0 full hash
    "%ae",   # 1 author email
    "%an",   # 2 author name
    "%aI",   # 3 author ISO date
    "%s",    # 4 subject
    "%b",    # 5 body
]) + "%x01"  # record separator


@dataclass
class RawCommit:
    hash: str
    author_email: str
    author_name: str
    commit_date: datetime
    subject: str
    body: str
    changed_files: list[str] = field(default_factory=list)


class GitRepo:
    """Thin wrapper around a local git repository."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def fetch(self, remote: str = "origin", branch: str | None = None) -> None:
        cmd = ["git", "-C", self.path, "fetch", remote]
        if branch:
            cmd.append(branch)
        logger.info("[git] fetch %s %s", remote, branch or "")
        subprocess.run(cmd, check=True, capture_output=True)

    def rev_parse_head(self, branch: str) -> str:
        """Return the full SHA of HEAD on *branch*."""
        result = subprocess.run(
            ["git", "-C", self.path, "rev-parse", f"origin/{branch}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def log_range(
        self,
        since_sha: str | None,
        branch: str,
        batch_size: int = 5000,
    ) -> Iterator[list[RawCommit]]:
        """Yield batches of RawCommit from *since_sha*..HEAD on *branch*."""
        ref = f"origin/{branch}"
        if since_sha:
            range_spec = f"{since_sha}..{ref}"
        else:
            range_spec = ref

        cmd = [
            "git", "-C", self.path, "log",
            "--format=" + _GIT_LOG_FORMAT,
            "--name-only",              # list changed files
            range_spec,
        ]
        logger.info("[git] log %s (since %s)", branch, since_sha or "beginning")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    check=True, timeout=3600)
        except subprocess.CalledProcessError as e:
            logger.error("[git] log failed: %s", e.stderr[:400])
            return

        output = result.stdout
        records = output.split("\x01")
        batch: list[RawCommit] = []

        for record in records:
            record = record.strip()
            if not record:
                continue
            rc = _parse_record(record)
            if rc:
                batch.append(rc)
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def clone_bare(self, remote: str, dest: Path, branch: str) -> None:
        """Clone remote as bare repo with single branch."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            logger.info("[git] %s already exists, skipping clone", dest)
            return
        logger.info("[git] cloning %s → %s (branch=%s)", remote, dest, branch)
        subprocess.run(
            ["git", "clone", "--bare", "--single-branch",
             "--branch", branch, remote, str(dest)],
            check=True, timeout=7200,
        )

    def exists(self) -> bool:
        return Path(self.path).exists()


def _parse_record(record: str) -> RawCommit | None:
    """Parse one git log record (NUL-separated fields + file list)."""
    # Split by the \x00 separators we put in the format
    # The body may contain \n; files follow after the last format field
    parts = record.split("\x00")
    if len(parts) < 6:
        return None

    commit_hash = parts[0].strip()
    if not commit_hash or len(commit_hash) < 7:
        return None

    author_email = parts[1].strip()
    author_name = parts[2].strip()

    date_raw = parts[3].strip()
    try:
        commit_date = datetime.fromisoformat(date_raw)
    except Exception:
        commit_date = datetime.now(timezone.utc)

    subject = parts[4].strip()
    # Body + file list are in parts[5]; files come after the body, separated by blank lines
    rest = parts[5] if len(parts) > 5 else ""
    body, changed_files = _split_body_and_files(rest)

    return RawCommit(
        hash=commit_hash,
        author_email=author_email,
        author_name=author_name,
        commit_date=commit_date,
        subject=subject,
        body=body,
        changed_files=changed_files,
    )


def _split_body_and_files(rest: str) -> tuple[str, list[str]]:
    """Split git log output into (body, [changed_files]).

    git --name-only appends file paths after a blank line following the body.
    """
    lines = rest.splitlines()
    body_lines: list[str] = []
    file_lines: list[str] = []
    in_files = False

    for line in lines:
        if not in_files and line == "" and body_lines:
            # Could be the separator before file list
            in_files = True
            continue
        if in_files:
            stripped = line.strip()
            if stripped and "/" in stripped or (stripped and "." in stripped):
                file_lines.append(stripped)
            elif stripped:
                # Not a file path — part of body
                in_files = False
                body_lines.append(line)
        else:
            body_lines.append(line)

    return "\n".join(body_lines).strip(), file_lines
