"""MCP server — dmesg/journal kernel event extractor (WBS 4.5).

Exposes two MCP tools:
  - extract_events: parse raw log text → list of KernelEvent dicts
  - summarize_events: LLM summary of extracted events (uses Navigator role)

Run as: python -m mcp_servers.dmesg_journal.server
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_servers.dmesg_journal.extractor import extract_events, KernelEvent

app = Server("dmesg-journal")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="extract_events",
            description=(
                "Parse raw dmesg or journald text and extract structured kernel events "
                "(oops, OOM, lockup, lockdep, panic, RCU stall)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_text": {
                        "type": "string",
                        "description": "Raw dmesg or journald log text.",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by event kind (oops,oom,softlockup,...). "
                                       "Omit to return all.",
                    },
                },
                "required": ["log_text"],
            },
        ),
        Tool(
            name="read_dmesg",
            description="Read the current kernel ring buffer via /proc/kmsg or dmesg command.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of lines to read (default 500).",
                        "default": 500,
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "extract_events":
        log_text = arguments.get("log_text", "")
        kind_filter = set(arguments.get("kinds", []))
        events = extract_events(log_text)
        if kind_filter:
            events = [e for e in events if e.kind.value in kind_filter]
        result = [e.to_dict() for e in events]
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "read_dmesg":
        import subprocess
        lines = int(arguments.get("lines", 500))
        try:
            out = subprocess.run(
                ["dmesg", "--time-format=iso", f"--lines={lines}"],
                capture_output=True, text=True, timeout=10,
            )
            text_out = out.stdout or out.stderr
        except Exception as exc:
            text_out = f"Error reading dmesg: {exc}"
        return [TextContent(type="text", text=text_out)]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
