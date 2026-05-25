"""Category B — Log & Event Parsing tools (M22).

Wraps dmesg extractor and sosreport parser as Tool objects. All tools accept
raw text or file paths and return human-readable summaries for LLM consumption.
"""

from __future__ import annotations

from agent.react.tool_registry import Tool

_ALL = frozenset({"kernel", "kernel+vmcore", "hardware", "change", "unknown"})


# ── tool functions ────────────────────────────────────────────────────────────

def _parse_dmesg(*, text: str, **_: object) -> str:
    """Extract structured kernel events from raw dmesg text."""
    from mcp_servers.dmesg_journal.extractor import extract_events
    events = extract_events(text)
    if not events:
        return "No kernel events detected in dmesg text."
    parts = []
    for e in events:
        d = e.to_dict()
        parts.append(f"[{d['kind'].upper()}] {d['summary']}")
        if d["call_trace"]:
            parts.append("  Call trace:")
            for frame in d["call_trace"][:10]:
                parts.append(f"    {frame}")
        if d.get("metadata"):
            parts.append(f"  metadata: {d['metadata']}")
    return "\n".join(parts)


def _parse_sosreport(*, path: str, **_: object) -> str:
    """Extract system summary from a sosreport archive or directory."""
    from mcp_servers.sosreport.parser import parse_sosreport
    try:
        summary = parse_sosreport(path)
    except Exception as exc:
        return f"Failed to parse sosreport at {path!r}: {exc}"
    d = summary.to_dict()
    lines = [
        f"hostname: {d['hostname']}",
        f"kernel_version: {d['kernel_version']}",
        f"olk_version_tag: {d['olk_version_tag']}",
        f"uname: {d['uname']}",
        f"uptime: {d['uptime']}",
    ]
    if d.get("dmesg_tail"):
        lines.append("--- dmesg tail (last 200 lines) ---")
        lines.append(d["dmesg_tail"][-2000:])
    return "\n".join(lines)


def _extract_call_trace(*, text: str, **_: object) -> str:
    """Extract call trace frames from a kernel panic, oops, or BUG message."""
    import re
    _FRAME_RE = re.compile(
        r"^\s+(?:\[<?[0-9a-f]+>?\]\s+)?(\w\S+\+0x[0-9a-f]+/0x[0-9a-f]+)",
    )
    _START_RE = re.compile(r"Call Trace:", re.IGNORECASE)
    _END_RE = re.compile(r"^---\[.*\]---$|^$")

    frames: list[str] = []
    in_trace = False
    for line in text.splitlines():
        if _START_RE.search(line):
            in_trace = True
            continue
        if in_trace:
            m = _FRAME_RE.match(line)
            if m:
                frames.append(m.group(1))
            elif frames and _END_RE.match(line.strip()):
                break
    if not frames:
        return "No call trace found in text."
    return "Call trace:\n" + "\n".join(f"  {f}" for f in frames)


# ── Tool objects ──────────────────────────────────────────────────────────────

PARSE_DMESG = Tool(
    name="parse_dmesg",
    description=(
        "Parse raw dmesg text and extract structured kernel events: "
        "OOM kills, oops, BUG, WARNING, soft/hard lockup, RCU stall, "
        "lockdep warnings, kernel panics. Returns event kind, summary, "
        "and call trace frames."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Raw dmesg or journald log text",
            },
        },
        "required": ["text"],
    },
    fn=_parse_dmesg,
    routes=_ALL,
)

PARSE_SOSREPORT = Tool(
    name="parse_sosreport",
    description=(
        "Parse a sosreport archive (.tar.xz) or extracted directory. "
        "Returns hostname, kernel version, OLK version tag, uptime, "
        "and dmesg tail."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to sosreport .tar.xz or directory",
            },
        },
        "required": ["path"],
    },
    fn=_parse_sosreport,
    routes=_ALL,
)

EXTRACT_CALL_TRACE = Tool(
    name="extract_call_trace",
    description=(
        "Extract kernel call trace frames from a raw text snippet "
        "(panic output, oops, BUG). Returns the ordered list of "
        "function frames in the call stack."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Raw text containing a kernel call trace",
            },
        },
        "required": ["text"],
    },
    fn=_extract_call_trace,
    routes=_ALL,
)

ALL_LOG_TOOLS = [PARSE_DMESG, PARSE_SOSREPORT, EXTRACT_CALL_TRACE]
