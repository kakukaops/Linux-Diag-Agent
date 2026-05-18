"""LKML fetcher — downloads mbox from lore.kernel.org (ADR-010).

Rate-limited: 4 concurrent lists, 3 retries with exponential backoff.
WARNING: 访问频率受控，每 list 请求间隔至少 1s，防止 IP 被封。
"""

from __future__ import annotations

import gzip
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# lore.kernel.org rate-limit: be conservative
_REQUEST_DELAY_S = 1.5   # seconds between requests per list
_TIMEOUT_S = 120


KERNEL_LISTS = [
    "linux-kernel",
    "linux-mm",
    "linux-fs",
    "linux-block",
    "linux-net",
    "linux-pci",
    "linux-arch",
    "linux-arm-kernel",
    "stable",
    "syzbot-bugs",
]

_BASE_URL = "https://lore.kernel.org"


class LoreFetcher:
    """Download mbox slices from lore.kernel.org."""

    def __init__(self, data_dir: Path, base_url: str = _BASE_URL) -> None:
        self._data_dir = data_dir
        self._base_url = base_url
        self._client = httpx.Client(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "linux-diag-agent/1.0 (kernel diagnostics research)"},
        )

    def fetch_mbox(
        self,
        list_name: str,
        since: datetime | None = None,
    ) -> bytes:
        """Download mbox for *list_name* since *since* (UTC).

        Returns raw mbox bytes (may be gzip-compressed from server).
        """
        params = {"x": "mbox"}
        if since:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self._base_url}/{list_name}/"
        logger.info("[lkml] Fetching %s since %s", list_name, since)
        return self._get(url, params=params)

    def save_mbox(
        self,
        list_name: str,
        data: bytes,
        month: datetime,
    ) -> Path:
        """Write mbox bytes (gzip-compressed) to data/lkml/<list>/<year>/<month>.mbox.gz."""
        out_dir = self._data_dir / "lkml" / list_name / str(month.year)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = out_dir / f"{month.strftime('%Y-%m')}.mbox.gz"
        with gzip.open(fname, "wb") as f:
            f.write(data)
        logger.debug("[lkml] Saved %d bytes to %s", len(data), fname)
        return fname

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=60),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        reraise=True,
    )
    def _get(self, url: str, params: dict | None = None) -> bytes:
        time.sleep(_REQUEST_DELAY_S)  # rate-limit guard
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
