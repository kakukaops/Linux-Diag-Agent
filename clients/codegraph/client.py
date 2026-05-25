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

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


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
        """Return True if the MCP server responds to initialize."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": "health",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "linux-diag-agent", "version": "1.0"},
                },
            }
            r = self._client.post(
                self._base,
                json=payload,
                headers=_MCP_HEADERS,
            )
            if r.status_code != 200:
                return False
            data = _parse_sse_response(r.text)
            return data is not None and "result" in data
        except Exception as exc:
            logger.warning("CodeGraph health check failed: %s", exc)
            return False

    def health_status(self) -> dict[str, Any]:
        """Return a detailed health status dict for observability."""
        import time
        t0 = time.monotonic()
        healthy = self.health_check()
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {
            "healthy": healthy,
            "latency_ms": latency_ms,
            "endpoint": self._base,
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
                self._base,
                json=payload,
                headers=_MCP_HEADERS,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CodeGraphError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise CodeGraphError(f"Request failed: {exc}") from exc

        data = _parse_sse_response(resp.text) or {}
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

        Returns a list of hit dicts with keys: file, snippet, score.
        """
        resolved = repo or self.resolve_repo(version_hint)
        params: dict[str, Any] = {"query": query, "limit": limit}
        if resolved:
            params["repos"] = [resolved]  # server expects array

        result = self._call("tools/call", {"name": "search_code", "arguments": params})
        return _parse_code_hits(result)

    def page_index_walk(
        self,
        file_path: str,
        symbol: str | None = None,
        repo: str | None = None,
        version_hint: str | None = None,
    ) -> dict[str, Any]:
        """High-level PageIndex walk for a kernel source file or symbol."""
        resolved = repo or self.resolve_repo(version_hint)
        params: dict[str, Any] = {"query": file_path}
        if resolved:
            params["repos"] = [resolved]

        result = self._call("tools/call", {"name": "search_code", "arguments": params})
        return {"raw": _get_text_content(result)}

    def passthrough(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Low-level passthrough: call any MCP tool directly."""
        return self._call("tools/call", {"name": tool_name, "arguments": arguments})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_sse_response(text: str) -> dict[str, Any] | None:
    """Extract JSON payload from an SSE response body (data: <json> lines)."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _get_text_content(result: Any) -> str:
    """Extract text from MCP content block."""
    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list):
            return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(result) if result else ""


def _parse_code_hits(result: Any) -> list[dict[str, Any]]:
    """Parse markdown text from search_code MCP result into hit dicts.

    The server returns a single text block formatted as:
        ### N. repo/path/to/file.c
        Language: C | Score: 12345.6
          L59: <code line>
          ...
    """
    import re
    text = _get_text_content(result)
    if not text or "No code matches found" in text:
        return []

    hits: list[dict[str, Any]] = []
    current_file: str | None = None
    current_score: float = 0.0
    snippet_lines: list[str] = []

    file_re = re.compile(r"^###\s+\d+\.\s+(.+)$")
    score_re = re.compile(r"Score:\s*([\d.]+)")
    line_re = re.compile(r"^\s+L\d+:\s+(.*)$")

    def flush() -> None:
        if current_file and snippet_lines:
            hits.append({
                "file": current_file,
                "snippet": "\n".join(snippet_lines[:8]),
                "score": current_score,
            })

    for line in text.splitlines():
        m = file_re.match(line)
        if m:
            flush()
            current_file = m.group(1).strip()
            current_score = 0.0
            snippet_lines = []
            continue
        m = score_re.search(line)
        if m and current_file:
            current_score = float(m.group(1))
            continue
        m = line_re.match(line)
        if m and current_file:
            snippet_lines.append(m.group(1))

    flush()
    return hits


# ── Module-level singleton ────────────────────────────────────────────────────

_client: CodeGraphClient | None = None


def get_codegraph_client() -> CodeGraphClient:
    global _client
    if _client is None:
        _client = CodeGraphClient()
    return _client
