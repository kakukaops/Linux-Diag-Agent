"""syzbot HTML scraper — syzkaller.appspot.com (M3 §3.3).

Rate limited: 2 concurrent, 3 retries, 1.5s delay between requests.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from ingest.syzbot.signature import stack_signature

logger = logging.getLogger(__name__)

_BASE = "https://syzkaller.appspot.com"
_DELAY_S = 1.5
_TIMEOUT_S = 60


@dataclass
class SyzbotCrash:
    syzbot_id: str
    title: str
    status: str = "open"
    subsystem: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    fix_commit: str | None = None
    stack_trace: str | None = None
    stack_signature: str | None = None
    reproducer_c: str | None = None
    reproducer_syz: str | None = None
    kernel_config_url: str | None = None


class SyzbotScraper:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._client = httpx.Client(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "linux-diag-agent/1.0"},
        )

    def list_crashes(self) -> list[dict[str, str]]:
        """Fetch the upstream crash list. Returns list of {id, title, status}."""
        html = self._get(f"{_BASE}/upstream")
        soup = BeautifulSoup(html, "html.parser")
        crashes = []
        for row in soup.select("table.list tr"):
            cols = row.find_all("td")
            if not cols:
                continue
            link = cols[0].find("a")
            if not link:
                continue
            href = link.get("href", "")
            crash_id = href.split("id=")[-1] if "id=" in href else ""
            if not crash_id:
                continue
            crashes.append({
                "id": crash_id,
                "title": link.get_text(strip=True),
                "status": cols[-1].get_text(strip=True) if len(cols) > 1 else "open",
            })
        return crashes

    def fetch_crash_detail(self, crash_id: str) -> SyzbotCrash | None:
        try:
            html = self._get(f"{_BASE}/bug?id={crash_id}")
        except Exception as exc:
            logger.warning("[syzbot] detail fetch failed %s: %s", crash_id, exc)
            return None

        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("h2") or soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else crash_id

        crash = SyzbotCrash(syzbot_id=crash_id, title=title)

        # Extract first <pre> block as stack trace
        pre = soup.find("pre")
        if pre:
            crash.stack_trace = pre.get_text()
            crash.stack_signature = stack_signature(crash.stack_trace)

        # Fix commit (look for "Fix:" or "Fixed by:" text patterns)
        import re
        fix_re = re.compile(r"(?:Fix:|Fixed by:)\s*([0-9a-f]{7,40})", re.IGNORECASE)
        for m in fix_re.finditer(html):
            crash.fix_commit = m.group(1)
            crash.status = "fixed"
            break

        # Reproducer links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "text/x-csrc" in href or href.endswith(".c"):
                crash.reproducer_c = href
            elif "syz" in href and href.endswith(".txt"):
                crash.reproducer_syz = href

        return crash

    def save_crash(self, crash: SyzbotCrash) -> None:
        """Persist crash data to data/syzbot/<id>/."""
        out = self._data_dir / "syzbot" / crash.syzbot_id
        out.mkdir(parents=True, exist_ok=True)
        import json
        (out / "metadata.json").write_text(
            json.dumps({
                "syzbot_id": crash.syzbot_id,
                "title": crash.title,
                "status": crash.status,
                "fix_commit": crash.fix_commit,
                "stack_signature": crash.stack_signature,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        if crash.stack_trace:
            (out / "report.txt").write_text(crash.stack_trace, encoding="utf-8")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=60))
    def _get(self, url: str) -> str:
        time.sleep(_DELAY_S)
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._client.close()
