"""Category F — vmcore forensics tools for the M22 ReAct loop (T-013 / T-015).

Only exposed on route 'kernel+vmcore'. Wraps drgn analyze_vmcore and
decode_stacktrace from mcp_servers/crash_forensics/.
"""

from __future__ import annotations

from agent.react.tool_registry import Tool

_KV = frozenset({"kernel+vmcore"})
_ALL = frozenset({"kernel", "kernel+vmcore", "hardware", "change", "unknown"})


# ── tool functions ────────────────────────────────────────────────────────────


def _analyze_vmcore(
    *,
    vmcore_path: str,
    query: str,
    vmlinux_path: str | None = None,
    kernel_version: str | None = None,
    **_: object,
) -> str:
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    result = analyze_vmcore(
        vmcore_path=vmcore_path,
        query=query,
        vmlinux_path=vmlinux_path,
        kernel_version=kernel_version,
    )
    if "error" in result:
        return f"[vmcore error] {result['error']}: {result.get('detail', '')}"

    lines = [f"vmcore analysis: query={query} vmcore={result.get('vmcore','')}"]

    if query == "all_stacks":
        total = result.get("total_tasks", 0)
        crashed = result.get("crashed_pid")
        lines.append(f"Total tasks: {total}, crashed PID: {crashed}")
        for task in result.get("per_task_stacks", [])[:5]:
            pid = task.get("pid")
            comm = task.get("comm", "?")
            frames = [f.get("name", "?") for f in task.get("frames", [])[:5]
                      if isinstance(f, dict) and "name" in f]
            lines.append(f"  PID {pid} ({comm}): {' → '.join(frames)}")

    elif query == "locks":
        blocked = result.get("tasks_blocked_on_lock", 0)
        lines.append(f"Tasks blocked on locks: {blocked}")
        for lock in result.get("held_locks", [])[:5]:
            lines.append(f"  PID {lock.get('blocked_pid')} blocked on: {lock.get('blocking_frames', [])}")

    elif query == "oom_context":
        victims = result.get("oom_victims", [])
        lines.append(f"OOM victims: {len(victims)}")
        for v in victims:
            lines.append(f"  PID {v.get('pid')} ({v.get('comm')}) oom_score_adj={v.get('oom_score_adj')}")
        for line in result.get("oom_dmesg_lines", [])[:10]:
            lines.append(f"  dmesg: {line.strip()}")

    elif query == "network_state":
        lines.append(f"Socket count estimate: {result.get('socket_count_estimate', 0)}")
        for state, count in result.get("tcp_states", {}).items():
            lines.append(f"  {state}: {count}")

    elif query == "memory_state":
        for zone in result.get("zones", []):
            wm = zone.get("watermarks", {})
            lines.append(
                f"  Zone {zone.get('name')}: present={zone.get('present_pages')} "
                f"managed={zone.get('managed_pages')} "
                f"watermarks={wm}"
            )
        for k, v in result.get("vmstat_summary", {}).items():
            lines.append(f"  {k}: {v}")

    errors = result.get("errors", [])
    if errors:
        lines.append(f"  [errors: {'; '.join(str(e) for e in errors[:3])}]")

    return "\n".join(lines)


def _fetch_debuginfo(
    *,
    kernel_version: str | None = None,
    sosreport_path: str | None = None,
    **_: object,
) -> str:
    from mcp_servers.crash_forensics.fetch_debuginfo import fetch_debuginfo
    result = fetch_debuginfo(kernel_version=kernel_version, sosreport_path=sosreport_path)
    if "error" in result:
        return f"[debuginfo error] {result['error']}: {result.get('detail', '')}"
    return (
        f"vmlinux found: {result['vmlinux_path']}\n"
        f"Source: {result['source']}\n"
        f"Kernel version: {result.get('kernel_version', 'unknown')}"
    )


def _decode_stacktrace(
    *,
    raw_trace: str,
    kernel_version: str | None = None,
    vmlinux_path: str | None = None,
    **_: object,
) -> str:
    from mcp_servers.crash_forensics.decode_stacktrace import decode_stacktrace
    result = decode_stacktrace(
        raw_trace=raw_trace,
        kernel_version=kernel_version,
        vmlinux_path=vmlinux_path,
    )
    if "error" in result:
        return f"[decode error] {result['error']}: {result.get('detail', '')}"
    frames = result.get("frames", [])
    if not frames:
        return "No frames decoded (trace may not contain raw addresses, or vmlinux mismatch)."
    lines = [f"Decoded {len(frames)} frames (vmlinux: {result.get('vmlinux_used', '?')}):"]
    for i, f in enumerate(frames[:20], 1):
        loc = f"{f.get('file', '')}:{f.get('line', '')}" if f.get("file") else ""
        offset = f.get("offset", "")
        lines.append(f"  #{i} {f.get('function', '?')}{'+' + offset if offset else ''} {loc}")
    return "\n".join(lines)


# ── Tool objects ──────────────────────────────────────────────────────────────


FETCH_DEBUGINFO = Tool(
    name="fetch_debuginfo",
    description=(
        "Locate vmlinux debug info for a given OLK kernel version. "
        "Must be called before analyze_vmcore or decode_stacktrace if vmlinux_path is unknown. "
        "Checks OLK build directories, /boot, debuginfo cache, and optional sosreport."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kernel_version": {
                "type": "string",
                "description": "Kernel version tag, e.g. 'OLK-6.6' or '5.10.0-153.oe2203sp3.x86_64'",
            },
            "sosreport_path": {
                "type": "string",
                "description": "Path to sosreport archive or directory (optional)",
            },
        },
        "required": [],
    },
    routes=_KV,
    fn=_fetch_debuginfo,
)

DECODE_STACKTRACE = Tool(
    name="decode_stacktrace",
    description=(
        "Translate raw kernel stack addresses to function+file:line using decode_stacktrace.sh. "
        "Use after extract_call_trace returns raw hex addresses that need symbol resolution. "
        "Requires vmlinux (call fetch_debuginfo first if path unknown)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "raw_trace": {
                "type": "string",
                "description": "Multi-line dmesg call trace with hex addresses",
            },
            "kernel_version": {
                "type": "string",
                "description": "e.g. 'OLK-6.6' — used to locate vmlinux automatically",
            },
            "vmlinux_path": {
                "type": "string",
                "description": "Explicit vmlinux path (optional, overrides auto-detection)",
            },
        },
        "required": ["raw_trace"],
    },
    routes=frozenset({"kernel+vmcore", "kernel", "unknown"}),
    fn=_decode_stacktrace,
)

ANALYZE_VMCORE = Tool(
    name="analyze_vmcore",
    description=(
        "Run a drgn forensics query against a kernel vmcore (crash dump). "
        "query must be one of: all_stacks (all task stack traces at crash time), "
        "locks (held mutex/rwsem analysis), oom_context (OOM kill victim + zone state), "
        "network_state (TCP socket summary), memory_state (zone watermarks + vmstat). "
        "Requires vmlinux (call fetch_debuginfo first)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "vmcore_path": {
                "type": "string",
                "description": "Absolute path to the vmcore file",
            },
            "query": {
                "type": "string",
                "enum": ["all_stacks", "locks", "oom_context", "network_state", "memory_state"],
                "description": "Which analysis to run",
            },
            "kernel_version": {
                "type": "string",
                "description": "e.g. 'OLK-6.6' — for auto-detecting vmlinux",
            },
            "vmlinux_path": {
                "type": "string",
                "description": "Explicit vmlinux path (optional)",
            },
        },
        "required": ["vmcore_path", "query"],
    },
    routes=_KV,
    fn=_analyze_vmcore,
)

ALL_VMCORE_TOOLS: list[Tool] = [
    FETCH_DEBUGINFO,
    DECODE_STACKTRACE,
    ANALYZE_VMCORE,
]
