"""CodeGraph HTTP MCP client (WBS 3.1 / ADR-009).

Connects to the codesearch MCP server at `codegraph.endpoint` via streamable-HTTP.
Provides two operations:
  - search_code: BM25 keyword search over the indexed kernel source
  - page_index_walk: structured PageIndex walk for a file/symbol
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from configs.config import get_config

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/health"
_MCP_PATH = "/mcp"


class CodeGraphError(Exception):
    """Raised when the CodeGraph MCP server returns an error."""


class CodeGraphClient:
    """Thin wrapper around the codesearch MCP streamable-HTTP transport."""

    def __init__(self, endpoint: str | None = None, timeout: float | None = None) -> None:
        cfg = get_config().codegraph
        self._base = (endpoint or cfg.endpoint).rstrip("/")
        self._timeout = timeout or cfg.timeout_seconds
        self._repo_map: dict[str, str] = dict(cfg.repo_map)
        self._client = httpx.Client(timeout=self._timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CodeGraphClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Repo resolution ───────────────────────────────────────────────────

    def resolve_repo(self, version_hint: str | None) -> str | None:
        """Map a kernel version string to the codesearch repo name."""
        if version_hint is None:
            return None
        return self._repo_map.get(version_hint)

    # ── Health check ─────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Return True if the server responds 200 on /health."""
        try:
            r = self._client.get(f"{self._base}{_HEALTH_PATH}")
            return r.status_code == 200
        except Exception as exc:
            logger.warning("CodeGraph health check failed: %s", exc)
            return False

    def health_status(self) -> dict[str, Any]:
        """Return a detailed health status dict for observability."""
        import time
        t0 = time.monotonic()
        try:
            r = self._client.get(f"{self._base}{_HEALTH_PATH}")
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            healthy = r.status_code == 200
            detail: dict[str, Any] = {}
            try:
                detail = r.json()
            except Exception:
                pass
            return {
                "healthy": healthy,
                "status_code": r.status_code,
                "latency_ms": latency_ms,
                "endpoint": self._base,
                "detail": detail,
            }
        except Exception as exc:
            return {
                "healthy": False,
                "error": str(exc),
                "endpoint": self._base,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            }

    def search_code_degraded(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Degraded-mode search: falls back to PG BM25 on kernel_commit.

        Used when CodeGraph is unreachable.
        """
        from sqlalchemy import text as sa_text
        from storage.pg.engine import get_engine

        engine = get_engine()
        tokens = [t for t in query.split() if len(t) >= 3]
        if not tokens:
            return []
        tsq = " | ".join(tokens[:10])
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa_text("""
                        SELECT hash, subject,
                               ts_rank_cd(body_tsv, to_tsquery('english', :q)) AS score
                          FROM kernel_commit
                         WHERE body_tsv @@ to_tsquery('english', :q)
                         ORDER BY score DESC
                         LIMIT :lim
                    """),
                    {"q": tsq, "lim": limit},
                ).fetchall()
            return [
                {"file": r.hash[:12], "snippet": r.subject, "score": float(r.score)}
                for r in rows
            ]
        except Exception as exc:
            logger.error("Degraded code search failed: %s", exc)
            return []

    # ── MCP JSON-RPC call ─────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _call(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC 2.0 request to /mcp and return result."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        try:
            resp = self._client.post(
                f"{self._base}{_MCP_PATH}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CodeGraphError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise CodeGraphError(f"Request failed: {exc}") from exc

        data = resp.json()
        if "error" in data:
            raise CodeGraphError(f"MCP error {data['error']}")
        return data.get("result")

    # ── Public API ────────────────────────────────────────────────────────

    def search_code(
        self,
        query: str,
        repo: str | None = None,
        version_hint: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """BM25 keyword search over indexed kernel source.

        Returns a list of hit dicts with keys: file, line, snippet, score.
        """
        resolved = repo or self.resolve_repo(version_hint)
        params: dict[str, Any] = {"query": query, "limit": limit}
        if resolved:
            params["repo"] = resolved

        result = self._call("tools/call", {"name": "search", "arguments": params})
        return _extract_hits(result)

    def page_index_walk(
        self,
        file_path: str,
        symbol: str | None = None,
        repo: str | None = None,
        version_hint: str | None = None,
    ) -> dict[str, Any]:
        """High-level PageIndex walk for a kernel source file or symbol.

        Returns a dict with keys: file, symbols, summary (when available).
        """
        resolved = repo or self.resolve_repo(version_hint)
        params: dict[str, Any] = {"file": file_path}
        if symbol:
            params["symbol"] = symbol
        if resolved:
            params["repo"] = resolved

        result = self._call("tools/call", {"name": "page_index", "arguments": params})
        if isinstance(result, dict):
            return result
        return {"raw": result}

    def passthrough(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Low-level passthrough: call any MCP tool directly."""
        return self._call("tools/call", {"name": tool_name, "arguments": arguments})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_hits(result: Any) -> list[dict[str, Any]]:
    """Normalise MCP result to a flat list of hit dicts."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    # MCP may wrap hits in {"content": [...]} or {"hits": [...]}
    if isinstance(result, dict):
        for key in ("hits", "content", "results"):
            if key in result and isinstance(result[key], list):
                return result[key]
    logger.debug("Unexpected CodeGraph result shape: %s", type(result))
    return []


# ── Module-level singleton ────────────────────────────────────────────────────

_client: CodeGraphClient | None = None


def get_codegraph_client() -> CodeGraphClient:
    global _client
    if _client is None:
        _client = CodeGraphClient()
    return _client
