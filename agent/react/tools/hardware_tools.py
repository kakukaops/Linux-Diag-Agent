"""Category H — Hardware diagnostic tools for the M22 ReAct loop (T-019).

Only exposed when route == 'hardware' (ADR-023). These tools wrap mcelog,
ras-mc-ctl, ipmitool, and dmidecode; all are read-only subprocess calls.
"""

from __future__ import annotations

from agent.react.tool_registry import Tool

_HW = frozenset({"hardware"})


# ── tool functions ────────────────────────────────────────────────────────────


def _get_mce_log(*, node: str = "local", since: str | None = None, **_: object) -> str:
    from mcp_servers.hardware.tools import get_mce_log
    result = get_mce_log(node=node, since=since)
    if "error" in result:
        return f"[mcelog error] {result['error']}: {result.get('detail', '')}"
    total = result["total_count"]
    if total == 0:
        return "No MCE events found. mcelog reports a clean machine."
    sev = result["severity_breakdown"]
    lines = [
        f"MCE events total={total} "
        f"(corrected={sev['corrected']}, uncorrected={sev['uncorrected']}, fatal={sev['fatal']})",
    ]
    for ev in result["events"][:10]:
        ts = ev.get("timestamp", "unknown time")
        etype = ev.get("error_type", "unknown")
        sev_str = ev.get("severity", "")
        cpu = ev.get("cpu", "?")
        bank = ev.get("bank", "?")
        dimm = ev.get("dimm_location", "")
        lines.append(
            f"  [{ts}] CPU{cpu} bank{bank}: {etype} severity={sev_str}"
            + (f" DIMM={dimm}" if dimm else "")
        )
    return "\n".join(lines)


def _get_edac_errors(*, node: str = "local", since: str | None = None, **_: object) -> str:
    from mcp_servers.hardware.tools import get_edac_errors
    result = get_edac_errors(node=node, since=since)
    if "error" in result:
        return f"[EDAC error] {result['error']}: {result.get('detail', '')}"
    s = result["summary"]
    trend = result.get("trend", "unknown")
    lines = [
        f"EDAC summary: CE={s['ce_count']} UE={s['ue_count']} "
        f"controllers={s['memory_controllers']} trend={trend}",
    ]
    for d in result.get("details_by_dimm", [])[:5]:
        lines.append(f"  DIMM {d.get('dimm','?')}: CE={d.get('ce_count',0)} UE={d.get('ue_count',0)}")
    if s["ue_count"] > 0:
        lines.append("WARNING: Uncorrected ECC errors indicate likely DIMM failure.")
    return "\n".join(lines)


def _get_ipmi_sel(
    *, node: str = "local", since: str | None = None,
    severity_filter: str = "all", **_: object
) -> str:
    from mcp_servers.hardware.tools import get_ipmi_sel
    result = get_ipmi_sel(node=node, since=since, severity_filter=severity_filter)
    if "error" in result:
        return f"[IPMI error] {result['error']}: {result.get('detail', '')}"
    lines = [f"Interpretation: {result.get('interpretation', 'N/A')}"]
    skew = result.get("bmc_clock_skew_seconds")
    if skew is not None:
        lines.append(f"BMC clock skew vs OS: {skew}s")
    for ev in result.get("events", [])[:15]:
        lines.append(
            f"  [{ev.get('timestamp','')}] {ev.get('sensor','')}: "
            f"{ev.get('event','')} ({ev.get('severity','')})"
        )
    return "\n".join(lines)


def _get_hardware_inventory(*, node: str = "local", **_: object) -> str:
    from mcp_servers.hardware.tools import get_hardware_inventory
    result = get_hardware_inventory(node=node)
    if "error" in result:
        return f"[dmidecode error] {result['error']}: {result.get('detail', '')}"
    sys = result.get("system", {})
    bios = result.get("bios", {})
    cpus = result.get("cpu", [])
    mem = result.get("memory", {})
    lines = [
        f"System: {sys.get('vendor','')} {sys.get('product','')} (serial={sys.get('serial','')})",
        f"BIOS: {bios.get('vendor','')} {bios.get('version','')} ({bios.get('release_date','')})",
    ]
    if cpus:
        c = cpus[0]
        lines.append(f"CPU: {c.get('model','')} x{len(cpus)} cores={c.get('cores','?')}")
    dimms = mem.get("dimms", [])
    lines.append(f"Memory: {len(dimms)} DIMMs installed (of {mem.get('total_slots',0)} slots)")
    for d in dimms[:8]:
        lines.append(
            f"  {d.get('slot','?')}: {d.get('size','?')} {d.get('manufacturer','?')} "
            f"{d.get('speed','?')} [{d.get('part_number','?')}]"
        )
    return "\n".join(lines)


def _parse_taint_flags(*, taint_value: str, **_: object) -> str:
    from mcp_servers.shared.taint_flags import decode_taint
    result = decode_taint(taint_value)
    flags = result.get("flags", [])
    high = result.get("high_priority_flags", [])
    if not flags:
        return "No taint flags found (kernel is clean)."
    lines = [f"Taint flags decoded ({len(flags)} active):"]
    for f in flags:
        marker = "[HIGH PRIORITY] " if f.get("high_priority") else ""
        lines.append(f"  {marker}{f['letter']}: {f['description']}")
        lines.append(f"    → {f['implications']}")
    if high:
        lines.append(f"\nHigh-priority flags: {', '.join(high)} — hardware investigation recommended")
    hints = result.get("search_narrowing_hints", [])
    if hints:
        lines.append(f"Search hints: {', '.join(hints)}")
    return "\n".join(lines)


# ── Tool objects ──────────────────────────────────────────────────────────────


GET_MCE_LOG = Tool(
    name="get_mce_log",
    description=(
        "Retrieve Machine Check Exception (MCE) records from mcelog daemon or /var/log/mcelog. "
        "Use when dmesg shows 'Machine Check' / 'Hardware Error' / 'mce:' lines. "
        "Returns decoded error type, DIMM location, and severity breakdown."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "hostname or 'local' (default)"},
            "since": {"type": "string", "description": "ISO datetime (optional); default last 24h"},
        },
        "required": [],
    },
    routes=_HW,
    fn=_get_mce_log,
)

GET_EDAC_ERRORS = Tool(
    name="get_edac_errors",
    description=(
        "Retrieve memory ECC error counts via ras-mc-ctl (rasdaemon) or EDAC sysfs. "
        "Shows corrected (CE) and uncorrected (UE) counts per DIMM with trend assessment. "
        "UE > 0 indicates likely DIMM failure requiring replacement."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "hostname or 'local' (default)"},
            "since": {"type": "string", "description": "time window (optional)"},
        },
        "required": [],
    },
    routes=_HW,
    fn=_get_edac_errors,
)

GET_IPMI_SEL = Tool(
    name="get_ipmi_sel",
    description=(
        "Read IPMI System Event Log via ipmitool. Captures hardware events the OS cannot see "
        "(pre-boot, BMC-detected). Correlates with kernel panic timestamps. "
        "Use severity_filter='critical_only' to focus on actionable events."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "hostname or 'local' (default)"},
            "since": {"type": "string", "description": "ISO datetime (optional)"},
            "severity_filter": {
                "type": "string",
                "enum": ["all", "critical_only"],
                "description": "Filter by severity (default: all)",
            },
        },
        "required": [],
    },
    routes=_HW,
    fn=_get_ipmi_sel,
)

GET_HARDWARE_INVENTORY = Tool(
    name="get_hardware_inventory",
    description=(
        "Retrieve hardware inventory (system model, BIOS version, CPU microcode, DIMM slots) "
        "via dmidecode. Use to identify hardware generation, check BIOS version against known "
        "MCE errata, and map DIMM slot labels from EDAC/mcelog to physical location."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "hostname or 'local' (default)"},
        },
        "required": [],
    },
    routes=_HW,
    fn=_get_hardware_inventory,
)

PARSE_TAINT_FLAGS = Tool(
    name="parse_taint_flags",
    description=(
        "Decode kernel taint flags from an integer bitmask or letter string (e.g. 'G M W' or '4096'). "
        "Returns human-readable flag descriptions and high-priority flags that indicate hardware issues "
        "(M = MCE, B = bad page). Use when dmesg shows a 'Tainted:' line."
    ),
    parameters={
        "type": "object",
        "properties": {
            "taint_value": {
                "type": "string",
                "description": "Integer bitmask (e.g. '4096') or letter string (e.g. 'G M W' or 'GMW')",
            },
        },
        "required": ["taint_value"],
    },
    routes=frozenset({"hardware", "kernel", "kernel+vmcore", "change", "unknown"}),
    fn=_parse_taint_flags,
)

ALL_HARDWARE_TOOLS: list[Tool] = [
    GET_MCE_LOG,
    GET_EDAC_ERRORS,
    GET_IPMI_SEL,
    GET_HARDWARE_INVENTORY,
    PARSE_TAINT_FLAGS,
]
