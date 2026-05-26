"""Thin atomgit API v5 client for openeuler/kernel issues.

Same API shape as gitee (v5) but:
  - host is atomgit.com (issue API shared with gitcode.com infra)
  - auth header is `Authorization: Bearer <token>` (NOT `token <t>` like gitee)
  - IDs are numeric (1..N), not alphanumeric

trust_env=False: bypass host HTTPS_PROXY (direct access required).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

logger = logging.getLogger(__name__)

_BASE = "https://atomgit.com/api/v5"
_REPO_PATH = "/repos/openeuler/kernel"
_RATE_RPM = 50  # conservative; raise if no throttling observed


def _limiter():
    from llm.rate_limit.sliding_window import get_endpoint_limiter
    return get_endpoint_limiter("atomgit.com", _RATE_RPM)


class AtomgitServerError(Exception):
    """Atomgit API returned 5xx for this issue (treat as deterministic skip)."""


class AtomgitFetcher:
    """Atomgit REST client. Returns parsed JSON, None on 404, raises
    AtomgitServerError on 5xx."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        headers = {"User-Agent": "linux-diag-agent/0.1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=_BASE, headers=headers, timeout=timeout, trust_env=False,
        )

    def __enter__(self) -> "AtomgitFetcher":
        return self

    def __exit__(self, *_) -> None:
        self._client.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=60),
        retry=retry_if_exception_type((httpx.TransportError, httpx.ReadTimeout)),
        reraise=True,
    )
    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Fetch a single issue by numeric ID. None on 404/410; raise on 5xx.

        Quirk: atomgit returns HTTP 400 with body `Issue Not Found` for some
        missing issues instead of 404. Treat that as 404.
        """
        _limiter().acquire()
        resp = self._client.get(f"{_REPO_PATH}/issues/{issue_id}")
        if resp.status_code in (404, 410):
            return None
        if resp.status_code == 400 and "issue not found" in resp.text.lower():
            return None
        if resp.status_code == 403 and "rate" in resp.text.lower():
            logger.warning("[atomgit] 403 rate limit on %s, retrying", issue_id)
            raise httpx.TransportError("rate_limited")
        if 500 <= resp.status_code < 600:
            raise AtomgitServerError(f"{resp.status_code} on {issue_id}")
        resp.raise_for_status()
        return resp.json()
