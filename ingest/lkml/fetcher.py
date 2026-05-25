"""LKML fetcher — Atom feed + per-message /raw download from lore.kernel.org.

Strategy: lore's ?x=mbox&since= returns HTML (unsupported). Instead:
  1. Fetch Atom feed with date query (?q=d:YYYYMMDD..&x=A, paginated via ?o=N)
  2. Download each message's raw RFC2822 bytes from /<list>/<msg-id>/raw
  3. Return concatenated mbox bytes to ingester (backward-compatible interface)

Rate-limit: 1.5s between requests. 访问频率受控，防止 IP 被封。
"""

from __future__ import annotations

import gzip
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

_REQUEST_DELAY_S = 1.5
_TIMEOUT_S = 120
_ATOM_PAGE_SIZE = 200  # lore returns 200 entries per Atom page
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_BASE_URL = "https://lore.kernel.org"

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
]


class LoreFetcher:
    """Download mbox slices from lore.kernel.org via Atom feed + /raw endpoint."""

    def __init__(self, data_dir: Path, base_url: str = _BASE_URL) -> None:
        self._data_dir = data_dir
        self._base_url = base_url
        self._client = httpx.Client(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "linux-diag-agent/1.0 (kernel diagnostics research)"},
        )

    def fetch_mbox(self, list_name: str, since: datetime | None = None) -> bytes:
        """Return concatenated raw mbox bytes for *list_name* since *since*.

        Fetches Atom feed entries (paginated), then downloads /raw for each.
        Returns empty bytes if no messages found.
        """
        date_str = since.strftime("%Y%m%d") if since else "19700101"
        query = f"d:{date_str}.."
        logger.info("[lkml] Fetching %s since %s via Atom+raw", list_name, date_str)

        message_urls = self._list_message_urls(list_name, query)
        if not message_urls:
            logger.info("[lkml] %s: no messages found", list_name)
            return b""

        logger.info("[lkml] %s: downloading %d messages", list_name, len(message_urls))
        parts: list[bytes] = []
        for url in message_urls:
            try:
                raw = self._fetch_raw(url)
                if raw:
                    parts.append(raw)
                    if not raw.endswith(b"\n"):
                        parts.append(b"\n")
            except Exception as exc:
                logger.debug("[lkml] %s: skipped %s — %s", list_name, url, exc)
        return b"".join(parts)

    def save_mbox(self, list_name: str, data: bytes, month: datetime) -> Path:
        """Write mbox bytes (gzip-compressed) to data/lkml/<list>/<year>/<month>.mbox.gz."""
        out_dir = self._data_dir / "lkml" / list_name / str(month.year)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = out_dir / f"{month.strftime('%Y-%m')}.mbox.gz"
        with gzip.open(fname, "wb") as f:
            f.write(data)
        logger.debug("[lkml] Saved %d bytes to %s", len(data), fname)
        return fname

    def fetch_raw_by_id(self, message_id: str) -> bytes:
        """Fetch one message's raw RFC2822 bytes by Message-ID via the
        list-agnostic /all/ archive. Returns b'' if not archived (404/410).

        Used by reference-driven backfill (ADR-025 L1): commit `Link:` trailers
        point to messages across many lists and years; /all/ addresses any of
        them by Message-ID, with no list or date-window restriction.
        """
        from urllib.parse import quote
        mid = message_id.strip().strip("<>")
        permalink = f"{self._base_url}/all/{quote(mid, safe='@._-+')}"
        return self._fetch_raw(permalink)

    def fetch_search_atom(self, keywords: str, offset: int = 0) -> bytes:
        """Full-text search lore's list-agnostic /all/ archive; return raw Atom
        bytes. Used by the L3 lore live-search retrieval route (ADR-025) —
        one search request per diagnosis, well within the rate limit.
        """
        return self._fetch_atom("all", keywords, offset)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _list_message_urls(self, list_name: str, query: str) -> list[str]:
        """Return all message permalink URLs from Atom feed matching *query*."""
        urls: list[str] = []
        offset = 0
        while True:
            atom = self._fetch_atom(list_name, query, offset)
            if not atom:
                break
            try:
                root = ET.fromstring(atom)
            except ET.ParseError as exc:
                logger.warning("[lkml] %s: Atom parse error at offset %d: %s", list_name, offset, exc)
                break

            entries = root.findall("a:entry", _ATOM_NS)
            if not entries:
                break
            for entry in entries:
                link = entry.find("a:link", _ATOM_NS)
                if link is not None:
                    href = link.get("href", "")
                    if href:
                        urls.append(href.rstrip("/"))
            if len(entries) < _ATOM_PAGE_SIZE:
                break
            offset += _ATOM_PAGE_SIZE
        return urls

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=60),
        # retry on any transient transport error (incl. RemoteProtocolError / chunked-read drops)
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _fetch_atom(self, list_name: str, query: str, offset: int) -> bytes:
        time.sleep(_REQUEST_DELAY_S)
        url = f"{self._base_url}/{list_name}/"
        resp = self._client.get(url, params={"q": query, "x": "A", "o": str(offset)})
        if resp.status_code == 404:
            return b""
        resp.raise_for_status()
        return resp.content

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=60),
        # retry on any transient transport error (incl. RemoteProtocolError / chunked-read drops)
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _fetch_raw(self, permalink: str) -> bytes:
        time.sleep(_REQUEST_DELAY_S)
        resp = self._client.get(f"{permalink}/raw")
        if resp.status_code in (404, 410):
            return b""
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
