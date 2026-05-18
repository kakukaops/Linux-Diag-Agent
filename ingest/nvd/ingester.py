"""NVD CVE ingester — JSON Feed v2 (M3 §3.5)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ingest.base import BaseIngester, Quarantine, RunReport
from ingest.nvd.extractor import extract_fix_commits

logger = logging.getLogger(__name__)

_NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_LINUX_CPE = "cpe:2.3:o:linux:linux_kernel:*"
_PAGE_SIZE = 2000
_DELAY_S = 6.0   # NVD: 5 req/30s without API key → ~6s per request
_BOOTSTRAP_SINCE = "2024-01-01T00:00:00.000"


class NvdIngester(BaseIngester):
    source_name = "nvd"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        import os
        self._api_key = os.environ.get("NVD_API_KEY", "")
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
        since = checkpoint.get("last_modified", _BOOTSTRAP_SINCE)
        newest = since
        start_index = 0

        while True:
            data = self._fetch_page(since, start_index)
            vulns = data.get("vulnerabilities", [])
            total = data.get("totalResults", 0)
            logger.info("[nvd] Fetched %d/%d CVEs (offset %d)", len(vulns), total, start_index)

            for item in vulns:
                cve_data = item.get("cve", {})
                try:
                    row = self._normalize(cve_data)
                    self._upsert_cve(row, report, quarantine)
                    lm = cve_data.get("lastModified", "")
                    if lm > newest:
                        newest = lm
                except Exception as exc:
                    cve_id = cve_data.get("id", "?")
                    quarantine.put(cve_id, cve_data, str(exc))
                    report.rows_failed += 1

            start_index += len(vulns)
            if start_index >= total or not vulns:
                break

        return {"last_modified": newest}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=_DELAY_S, max=120))
    def _fetch_page(self, since: str, start_index: int) -> dict:
        time.sleep(_DELAY_S)
        params: dict = {
            "virtualMatchString": _LINUX_CPE,
            "lastModStartDate": since,
            "startIndex": start_index,
            "resultsPerPage": _PAGE_SIZE,
        }
        if self._api_key:
            params["apiKey"] = self._api_key
        resp = self._client.get(_NVD_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    def _normalize(self, cve_data: dict) -> dict:
        cve_id = cve_data.get("id", "")
        desc = next(
            (d["value"] for d in cve_data.get("descriptions", []) if d.get("lang") == "en"),
            None,
        )
        metrics = cve_data.get("metrics", {})
        cvss3 = next(iter(metrics.get("cvssMetricV31", []) or metrics.get("cvssMetricV30", [])), {})
        cvss_data = cvss3.get("cvssData", {})
        refs = [{"url": r.get("url"), "tags": r.get("tags", [])}
                for r in cve_data.get("references", [])]
        fix_commits = extract_fix_commits(cve_data)

        return {
            "cve_id": cve_id,
            "published_at": _parse_dt(cve_data.get("published")),
            "last_modified_at": _parse_dt(cve_data.get("lastModified")),
            "description": desc,
            "cvss_v3_score": cvss_data.get("baseScore"),
            "cvss_v3_vector": cvss_data.get("vectorString"),
            "references": refs,
            "fix_commits": fix_commits,
        }

    def _upsert_cve(self, row: dict, report: RunReport, quarantine: Quarantine) -> None:
        from sqlalchemy import text
        import json
        sql = text("""
            INSERT INTO cve (cve_id, published_at, last_modified_at, description,
                             cvss_v3_score, cvss_v3_vector, references)
            VALUES (:cve_id, :published_at, :last_modified_at, :description,
                    :cvss_v3_score, :cvss_v3_vector, :references::jsonb)
            ON CONFLICT (cve_id) DO UPDATE SET
                last_modified_at = EXCLUDED.last_modified_at,
                cvss_v3_score = EXCLUDED.cvss_v3_score,
                description = COALESCE(EXCLUDED.description, cve.description)
        """)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {
                    **row,
                    "references": json.dumps(row["references"]),
                })
                conn.commit()
            if result.rowcount > 0:
                report.rows_inserted += 1
            else:
                report.rows_updated += 1
        except Exception as exc:
            quarantine.put(row["cve_id"], row, str(exc))
            report.rows_failed += 1


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
