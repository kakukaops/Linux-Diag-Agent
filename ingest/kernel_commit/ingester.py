"""Kernel commit ETL — dual-repo model (ADR-018).

Phase A: OLK kernel repo (main) — OLK-6.6 + OLK-5.10
Phase B: linux-stable.git (auxiliary) — master branch (mainline history)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ingest.base import BaseIngester, Quarantine, RunReport
from ingest.kernel_commit.git_wrapper import GitRepo, RawCommit
from ingest.kernel_commit.olk_inclusion import OlkInclusionInfo, parse_olk_inclusion
from ingest.kernel_commit.subsystem_inference import infer_subsystem
from ingest.kernel_commit.trailer_parser import CommitTrailers, parse_trailers

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5000


class KernelCommitIngester(BaseIngester):
    source_name = "kernel_commit"

    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        from configs.config import get_config
        cfg = get_config().ingestion.kernel_commit

        last_sha: dict[str, str] = checkpoint.get("last_sha_per_branch", {})
        new_sha: dict[str, str] = dict(last_sha)

        # ── Phase A: OLK repos ───────────────────────────────────────────
        for repo_cfg in cfg.olk_repos:
            branch = repo_cfg.branch
            repo_path = repo_cfg.local_path
            logger.info("[kernel_commit] Phase A: %s at %s", branch, repo_path)

            repo = GitRepo(repo_path)
            if not repo.exists():
                logger.warning("[kernel_commit] OLK repo not found: %s. "
                               "Run bootstrap first.", repo_path)
                continue

            try:
                repo.fetch(remote="origin", branch=branch)
            except Exception as exc:
                logger.error("[kernel_commit] fetch %s failed: %s", branch, exc)
                continue

            since_sha = last_sha.get(branch)
            for batch in repo.log_range(since_sha, branch, batch_size=_BATCH_SIZE):
                for rc in batch:
                    try:
                        self._process_olk_commit(rc, branch, report, quarantine)
                    except Exception as exc:
                        quarantine.put(rc.hash, rc.body[:200], str(exc))
                        report.rows_failed += 1

                # Update checkpoint after each batch
                if batch:
                    new_sha[branch] = batch[0].hash  # most recent

            # Set final HEAD sha
            try:
                head = repo.rev_parse_head(branch)
                new_sha[branch] = head
            except Exception:
                pass

        # ── Phase B: linux-stable.git (mainline) ─────────────────────────
        mainline_cfg = cfg.mainline_repo
        mainline_path = mainline_cfg.local_path
        logger.info("[kernel_commit] Phase B: mainline at %s", mainline_path)

        repo = GitRepo(mainline_path)
        if repo.exists():
            try:
                repo.fetch(remote="origin", branch="master")
            except Exception as exc:
                logger.error("[kernel_commit] mainline fetch failed: %s", exc)

            since_sha = last_sha.get("mainline")
            for batch in repo.log_range(since_sha, "master", batch_size=_BATCH_SIZE):
                for rc in batch:
                    try:
                        self._process_mainline_commit(rc, report, quarantine)
                    except Exception as exc:
                        quarantine.put(rc.hash, rc.body[:200], str(exc))
                        report.rows_failed += 1
                if batch:
                    new_sha["mainline"] = batch[0].hash

            try:
                head = repo.rev_parse_head("master")
                new_sha["mainline"] = head
            except Exception:
                pass
        else:
            logger.warning("[kernel_commit] linux-stable not found: %s. "
                           "Run bootstrap first.", mainline_path)

        return {"last_sha_per_branch": new_sha}

    # ── OLK commit processing ─────────────────────────────────────────────

    def _process_olk_commit(
        self,
        rc: RawCommit,
        branch: str,
        report: RunReport,
        quarantine: Quarantine,
    ) -> None:
        inclusion = parse_olk_inclusion(rc.subject, rc.body)
        if inclusion.kind == "merge":
            return  # skip MR merge commits (~7.6%)

        trailers = parse_trailers(rc.body)
        subsystem = infer_subsystem(rc.changed_files)

        row = {
            "hash": rc.hash,
            "author_email": rc.author_email,
            "author_name": rc.author_name,
            "commit_date": rc.commit_date,
            "subject": rc.subject,
            "body": rc.body,
            "origin": "olk",
            "olk_inclusion_type": inclusion.tag,
            "upstream_commit": inclusion.best_upstream_sha if inclusion.is_backport else None,
            "subsystem": subsystem,
            "fixes_refs": trailers.fixes_refs or None,
            "reported_by": trailers.reported_by or None,
            "closes_refs": trailers.closes_refs or None,
            "branch": branch,   # used to update affected_versions
        }
        self._upsert_commit(row, branch, report, quarantine)

    # ── mainline commit processing ────────────────────────────────────────

    def _process_mainline_commit(
        self,
        rc: RawCommit,
        report: RunReport,
        quarantine: Quarantine,
    ) -> None:
        trailers = parse_trailers(rc.body)
        subsystem = infer_subsystem(rc.changed_files)

        row = {
            "hash": rc.hash,
            "author_email": rc.author_email,
            "author_name": rc.author_name,
            "commit_date": rc.commit_date,
            "subject": rc.subject,
            "body": rc.body,
            "origin": "mainline",
            "olk_inclusion_type": None,
            "upstream_commit": None,
            "subsystem": subsystem,
            "fixes_refs": trailers.fixes_refs or None,
            "reported_by": trailers.reported_by or None,
            "closes_refs": trailers.closes_refs or None,
            "branch": "mainline",
        }
        self._upsert_commit(row, "mainline", report, quarantine)

    # ── DB upsert ─────────────────────────────────────────────────────────

    def _upsert_commit(
        self,
        row: dict,
        branch: str,
        report: RunReport,
        quarantine: Quarantine,
    ) -> None:
        sql = text("""
            INSERT INTO kernel_commit
                (hash, author_email, author_name, commit_date, subject, body,
                 origin, olk_inclusion_type, upstream_commit, subsystem,
                 fixes_refs, reported_by, closes_refs, affected_versions)
            VALUES
                (:hash, :author_email, :author_name, :commit_date, :subject, :body,
                 :origin, :olk_inclusion_type, :upstream_commit, :subsystem,
                 :fixes_refs, :reported_by, :closes_refs, ARRAY[:branch])
            ON CONFLICT (hash) DO UPDATE SET
                -- Update mutable fields; append new branch to affected_versions if not present
                upstream_commit = COALESCE(EXCLUDED.upstream_commit, kernel_commit.upstream_commit),
                olk_inclusion_type = COALESCE(EXCLUDED.olk_inclusion_type, kernel_commit.olk_inclusion_type),
                subsystem = COALESCE(EXCLUDED.subsystem, kernel_commit.subsystem),
                affected_versions = (
                    SELECT ARRAY(
                        SELECT DISTINCT unnest(
                            kernel_commit.affected_versions || ARRAY[:branch]
                        )
                    )
                )
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {
                    **row,
                    "branch": branch,
                    "fixes_refs": row["fixes_refs"],
                    "reported_by": row["reported_by"],
                    "closes_refs": row["closes_refs"],
                })
                conn.commit()
            if result.rowcount > 0:
                report.rows_inserted += 1
            else:
                report.rows_updated += 1
        except Exception as exc:
            quarantine.put(row["hash"], row, str(exc))
            report.rows_failed += 1
