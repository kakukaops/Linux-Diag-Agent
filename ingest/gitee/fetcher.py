"""Thin gitee API v5 client for openeuler/kernel issues.

Anonymous access: 60 req/min hard cap (observed via x-ratelimit-* headers).
Shared endpoint limiter prevents bursts under any concurrent caller.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

logger = logging.getLogger(__name__)

_BASE = "https://gitee.com/api/v5"
_REPO_PATH = "/repos/openeuler/kernel"
# 60 rpm hard cap; use 50 to leave headroom for occasional retries.
_RATE_RPM = 50


def _limiter():
    from llm.rate_limit.sliding_window import get_endpoint_limiter
    return get_endpoint_limiter("gitee.com", _RATE_RPM)


class GiteeServerError(Exception):
    """Gitee API returned 5xx for this issue (deterministic data bug — older
    issues whose HTML page works fine but JSON API errors). Not retried.
    """


class GiteeFetcher:
    """Anonymous gitee REST client. Returns parsed JSON, None on 404,
    raises GiteeServerError on 5xx."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        headers = {"User-Agent": "linux-diag-agent/0.1"}
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx.Client(base_url=_BASE, headers=headers, timeout=timeout)

    def __enter__(self) -> "GiteeFetcher":
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
        """Fetch a single issue by ID. None on 404/410; raise on 5xx."""
        _limiter().acquire()
        resp = self._client.get(f"{_REPO_PATH}/issues/{issue_id}")
        if resp.status_code in (404, 410):
            return None
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            logger.warning("[gitee] 403 rate limit on %s, retrying", issue_id)
            raise httpx.TransportError("rate_limited")
        if 500 <= resp.status_code < 600:
            raise GiteeServerError(f"{resp.status_code} on {issue_id}")
        resp.raise_for_status()
        return resp.json()
