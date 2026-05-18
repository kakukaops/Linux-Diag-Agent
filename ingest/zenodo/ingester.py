"""Zenodo dataset ingester — one-time / quarterly refresh (M3 §3.4)."""

from __future__ import annotations

import csv
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ingest.base import BaseIngester, Quarantine, RunReport

logger = logging.getLogger(__name__)

_ZENODO_API = "https://zenodo.org/api/records"
_DELAY_S = 2.0


class ZenodoIngester(BaseIngester):
    source_name = "zenodo"

    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        from configs.config import get_config
        cfg = get_config()
        doi_list = cfg.ingestion.zenodo.doi_list if hasattr(cfg.ingestion, "zenodo") else []

        if not doi_list:
            logger.info("[zenodo] No DOIs configured, skipping")
            return checkpoint

        local_version = checkpoint.get("dataset_version", "")
        client = httpx.Client(timeout=60)

        for doi in doi_list:
            record_id = doi.split("zenodo.")[-1]
            try:
                resp = client.get(f"{_ZENODO_API}/{record_id}")
                resp.raise_for_status()
                record = resp.json()
                version = str(record.get("id", record_id))
                if version == local_version:
                    logger.info("[zenodo] %s: already up to date (v%s)", doi, version)
                    continue

                # Download the dataset
                files = record.get("files", [])
                for f in files:
                    if f.get("key", "").endswith(".zip"):
                        url = f["links"]["self"]
                        local_zip = self._data_dir / "zenodo" / f"{version}.zip"
                        self._download(client, url, local_zip)
                        self._import_zip(local_zip, report, quarantine)
                        break

                return {
                    "dataset_version": version,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                logger.error("[zenodo] %s failed: %s", doi, exc)

        return checkpoint

    def _download(self, client: httpx.Client, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info("[zenodo] Downloading %s → %s", url, dest)
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(65536):
                    f.write(chunk)

    def _import_zip(self, zip_path: Path, report: RunReport, quarantine: Quarantine) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for name in names:
                if name.endswith("bug_fix_pairs.json"):
                    with zf.open(name) as f:
                        pairs = json.load(f)
                    self._import_pairs(pairs, report, quarantine)
                elif name.endswith("commits.csv"):
                    with zf.open(name) as f:
                        reader = csv.DictReader(line.decode() for line in f)
                        self._import_commits(list(reader), report, quarantine)

    def _import_pairs(self, pairs: list[dict], report: RunReport, quarantine: Quarantine) -> None:
        from sqlalchemy import text
        for pair in pairs:
            commit_hash = pair.get("fix_commit", "")
            bug_ref = pair.get("bug_id", "")
            if not commit_hash or not bug_ref:
                continue
            # link_commit_bug requires a real bug.id — skip if bug not yet in DB
            sql = text("""
                INSERT INTO link_commit_bug (commit_hash, bug_id, link_type, confidence, source)
                SELECT :hash, b.id, 'fixes', 0.92, 'zenodo_dataset'
                  FROM bug b WHERE b.external_id = :bug_ref
                ON CONFLICT DO NOTHING
            """)
            try:
                with self._engine.connect() as conn:
                    conn.execute(sql, {"hash": commit_hash, "bug_ref": str(bug_ref)})
                    conn.commit()
                report.rows_inserted += 1
            except Exception as exc:
                quarantine.put(commit_hash, pair, str(exc))
                report.rows_failed += 1

    def _import_commits(self, rows: list[dict], report: RunReport, quarantine: Quarantine) -> None:
        from sqlalchemy import text
        for row in rows:
            h = row.get("commit_hash", "")
            if not h:
                continue
            sql = text("""
                INSERT INTO kernel_commit (hash, subject, commit_date, origin)
                VALUES (:hash, :subject, :date, 'mainline')
                ON CONFLICT (hash) DO NOTHING
            """)
            try:
                with self._engine.connect() as conn:
                    conn.execute(sql, {
                        "hash": h,
                        "subject": row.get("subject", ""),
                        "date": _parse_dt(row.get("date")),
                    })
                    conn.commit()
                report.rows_inserted += 1
            except Exception as exc:
                quarantine.put(h, row, str(exc))
                report.rows_failed += 1


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)
