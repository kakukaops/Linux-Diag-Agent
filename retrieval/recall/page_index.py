"""PageIndex walkthrough — high-level wrapper + low-level passthrough (WBS 3.8).

High-level: given a file path (or symbol), returns a structured summary from
the CodeGraph PageIndex MCP tool.

Low-level passthrough: callers can pass any MCP tool name + arguments directly.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def walk_file(
    file_path: str,
    symbol: str | None = None,
    version_hint: str | None = None,
) -> dict[str, Any]:
    """High-level PageIndex walk for a kernel source file or symbol.

    Returns a dict with keys: file, symbols, summary, raw.
    Returns empty dict if CodeGraph is unavailable.
    """
    try:
        from clients.codegraph.client import get_codegraph_client, CodeGraphError
        client = get_codegraph_client()
        if not client.health_check():
            return {}
        result = client.page_index_walk(
            file_path=file_path,
            symbol=symbol,
            version_hint=version_hint,
        )
        return result
    except Exception as exc:
        logger.warning("PageIndex walk_file failed for %s: %s", file_path, exc)
        return {}


def passthrough(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Low-level MCP passthrough — call any CodeGraph tool directly."""
    from clients.codegraph.client import get_codegraph_client
    return get_codegraph_client().passthrough(tool_name, arguments)
