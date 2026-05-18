"""MCP server — sosreport system summary extractor (WBS 4.6).

Exposes one MCP tool:
  - parse_sosreport: given a path to a .tar.xz or extracted directory,
    returns a SystemSummary dict (kernel_version, hostname, dmesg_tail, etc.)

Run as: python -m mcp_servers.sosreport.server
"""

from __future__ import annotations

import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_servers.sosreport.parser import parse_sosreport

app = Server("sosreport")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="parse_sosreport",
            description=(
                "Parse a sosreport archive (.tar.xz) or extracted directory and return "
                "a SystemSummary: kernel_version, hostname, uptime, uname, dmesg_tail, "
                "and olk_version_tag (OLK-6.6/OLK-5.10/mainline/unknown)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to sosreport .tar.xz or directory.",
                    },
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "parse_sosreport":
        path = arguments.get("path", "")
        try:
            summary = parse_sosreport(path)
            return [TextContent(type="text", text=json.dumps(summary.to_dict(), indent=2))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
