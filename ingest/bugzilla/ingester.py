"""Bugzilla.kernel.org ingester — REST API v1 (ADR-011).

Only ingests bugzilla.kernel.org (not Red Hat BZ).
No API key required; rate limit ~2 concurrent requests.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ingest.base import BaseIngester, Quarantine, RunReport
from ingest.bugzilla.normalizer import normalize_resolution, normalize_severity, normalize_status

logger = logging.getLogger(__name__)

_BZ_BASE = "https://bugzilla.kernel.org/rest"
_BATCH_SIZE = 100
_REQUEST_DELAY_S = 2.0
_BOOTSTRAP_SINCE = "2024-05-14T00:00:00Z"


class BugzillaIngester(BaseIngester):
    source_name = "bugzilla"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._client = httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "linux-diag-agent/1.0"},
        )

    def incremental(
        self,
        checkpoint: dict[str, Any],
        report: RunReport,
        quarantine: Quarantine,
    ) -> dict[str, Any]:
        since = checkpoint.get("last_change_time", _BOOTSTRAP_SINCE)
        offset = 0
        newest: str = since

        while True:
            bugs = self._fetch_batch(since, offset)
            if not bugs:
                break
            for raw in bugs:
                try:
                    row = self._normalize(raw)
                    self._upsert_bug(row, report, quarantine)
                    if raw.get("last_change_time", "") > newest:
                        newest = raw["last_change_time"]
                except Exception as exc:
                    quarantine.put(str(raw.get("id", "?")), raw, str(exc))
                    report.rows_failed += 1

            if len(bugs) < _BATCH_SIZE:
                break
            offset += _BATCH_SIZE
            time.sleep(_REQUEST_DELAY_S)

        return {"last_change_time": newest}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=60))
    def _fetch_batch(self, since: str, offset: int) -> list[dict]:
        time.sleep(_REQUEST_DELAY_S)
        params = {
            "last_change_time": since[:10],  # Bugzilla REST expects YYYY-MM-DD
            "limit": _BATCH_SIZE,
            "offset": offset,
            "include_fields": ",".join([
                "id", "summary", "status", "resolution", "severity",
                "product", "component", "creator", "assigned_to",
                "creation_time", "last_change_time", "see_also",
                "depends_on", "blocks",
            ]),
        }
        resp = self._client.get(f"{_BZ_BASE}/bug", params=params)
        resp.raise_for_status()
        return resp.json().get("bugs", [])

    def _normalize(self, raw: dict) -> dict:
        return {
            "source": "bugzilla_kernel",
            "external_id": str(raw["id"]),
            "title": raw.get("summary", ""),
            "status": normalize_status(raw.get("status", "")),
            "resolution": normalize_resolution(raw.get("resolution", "")),
            "severity": normalize_severity(raw.get("severity", "")),
            "component": raw.get("component", ""),
            "reporter": raw.get("creator", ""),
            "assignee": raw.get("assigned_to", ""),
            "created_at": _parse_dt(raw.get("creation_time")),
            "updated_at": _parse_dt(raw.get("last_change_time")),
            "description": None,  # fetched separately if needed
        }

    def _upsert_bug(self, row: dict, report: RunReport, quarantine: Quarantine) -> None:
        from sqlalchemy import text
        sql = text("""
            INSERT INTO bug (source, external_id, title, status, resolution, severity,
                             component, reporter, assignee, created_at, updated_at, description)
            VALUES (:source, :external_id, :title, :status, :resolution, :severity,
                    :component, :reporter, :assignee, :created_at, :updated_at, :description)
            ON CONFLICT (source, external_id) DO UPDATE SET
                status = EXCLUDED.status,
                resolution = EXCLUDED.resolution,
                updated_at = EXCLUDED.updated_at
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, row)
                conn.commit()
            if result.rowcount > 0:
                report.rows_inserted += 1
            else:
                report.rows_updated += 1
        except Exception as exc:
            quarantine.put(row["external_id"], row, str(exc))
            report.rows_failed += 1


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
