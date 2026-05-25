"""M11 hardware diagnostic tool implementations (T-019).

Four read-only subprocess wrappers: mcelog, ras-mc-ctl, ipmitool, dmidecode.
All tools accept `node` ('local' or hostname); remote execution goes via SSH.
On tool-not-installed or permission error, returns a structured error dict
rather than raising — graceful degradation is required (ADR-023 D5).
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from typing import Any


_TIMEOUT = 30  # seconds per subprocess call


# ── Subprocess helper ─────────────────────────────────────────────────────────


def _run(cmd: list[str], node: str = "local") -> tuple[int, str, str]:
    """Run cmd, optionally via SSH. Returns (returncode, stdout, stderr)."""
    if node and node != "local":
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node] + cmd
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_TIMEOUT,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return (-1, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return (-2, "", f"timeout after {_TIMEOUT}s")
    except Exception as exc:
        return (-3, "", str(exc))


# ── get_mce_log ───────────────────────────────────────────────────────────────


def get_mce_log(node: str = "local", since: str | None = None) -> dict[str, Any]:
    """Retrieve MCE records from mcelog.

    Tries `mcelog --client` (daemon mode) first; falls back to parsing
    /var/log/mcelog for hosts without daemon.
    """
    rc, out, err = _run(["mcelog", "--client"], node)
    if rc == -1:
        return {"error": "mcelog_not_installed", "detail": err}
    if rc != 0 and "not running" in err.lower():
        # Try reading log file directly
        rc2, out2, _ = _run(["cat", "/var/log/mcelog"], node)
        if rc2 != 0:
            return {"error": "mcelog_not_running",
                    "detail": "mcelog daemon is not running and /var/log/mcelog not readable; "
                              "enable mcelog or rasdaemon"}
        out = out2

    events = _parse_mcelog_output(out)
    summary = _mce_severity_summary(events)
    return {
        "events": events,
        "total_count": len(events),
        "severity_breakdown": summary,
    }


def _parse_mcelog_output(text: str) -> list[dict]:
    """Parse mcelog text output into structured event dicts."""
    from mcp_servers.hardware.mce_codes import decode_mci_status

    events: list[dict] = []
    current: dict[str, Any] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                events.append(current)
                current = {}
            continue

        if line.startswith("Hardware event"):
            if current:
                events.append(current)
            current = {}
        elif m := re.match(r"CPU (\d+) BANK (\d+)", line):
            current["cpu"] = int(m.group(1))
            current["bank"] = int(m.group(2))
        elif m := re.match(r"TIME (\d+) (.+)", line):
            ts_int = int(m.group(1))
            current["timestamp"] = datetime.fromtimestamp(
                ts_int, tz=timezone.utc
            ).isoformat()
        elif m := re.match(r"MCi_STATUS\s+(0x[0-9a-fA-F]+)", line):
            raw_val = int(m.group(1), 16)
            decoded = decode_mci_status(raw_val)
            current["mci_status"] = m.group(1)
            current["error_type"] = decoded["error_type"]
            current["severity"] = decoded["severity"]
            current["fatal"] = decoded["fatal"]
        elif m := re.match(r"MCi_ADDR\s+(0x[0-9a-fA-F]+)", line):
            current["mci_addr"] = m.group(1)
        elif "DIMM" in line or "SLOT" in line.upper():
            current.setdefault("dimm_location", line)

        if line and "error" in line.lower():
            current.setdefault("raw_log", "")
            current["raw_log"] = (current.get("raw_log", "") + line + "\n")

    if current:
        events.append(current)

    return [e for e in events if e]  # drop empty


def _mce_severity_summary(events: list[dict]) -> dict:
    s = {"corrected": 0, "uncorrected": 0, "fatal": 0}
    for e in events:
        sev = e.get("severity", "corrected")
        if sev in s:
            s[sev] += 1
    return s


# ── get_edac_errors ───────────────────────────────────────────────────────────


def get_edac_errors(node: str = "local", since: str | None = None) -> dict[str, Any]:
    """Retrieve ECC errors via ras-mc-ctl (rasdaemon) or edac-util."""
    # Try ras-mc-ctl first (modern, rasdaemon-based)
    rc, out, err = _run(["ras-mc-ctl", "--summary"], node)
    if rc == -1:
        # Try edac-util (older sysfs-based)
        rc, out, err = _run(["edac-util", "-s", "4"], node)
        if rc == -1:
            # Last resort: read sysfs directly
            return _edac_sysfs(node)

    return _parse_ras_mc_summary(out, node)


def _parse_ras_mc_summary(text: str, node: str) -> dict[str, Any]:
    """Parse ras-mc-ctl --summary output."""
    ce_count = 0
    ue_count = 0
    mcs = set()
    details: list[dict] = []

    for line in text.splitlines():
        if m := re.search(r"mc(\d+)", line, re.IGNORECASE):
            mcs.add(m.group(1))
        if m := re.search(r"(\d+)\s+CE\b", line):
            ce_count += int(m.group(1))
        if m := re.search(r"(\d+)\s+UE\b", line):
            ue_count += int(m.group(1))
        if re.search(r"csrow|dimm|channel", line, re.IGNORECASE):
            details.append({"raw": line.strip()})

    # Also get per-DIMM errors from --errors
    rc, out2, _ = _run(["ras-mc-ctl", "--errors"], node)
    per_dimm = _parse_ras_mc_errors(out2) if rc == 0 else []

    trend = "stable"
    if ue_count > 0:
        trend = "critical"
    elif ce_count > 100:
        trend = "increasing"

    return {
        "summary": {
            "ce_count": ce_count,
            "ue_count": ue_count,
            "memory_controllers": len(mcs),
            "csrows_with_errors": [d.get("raw", "") for d in details[:5]],
        },
        "details_by_dimm": per_dimm,
        "trend": trend,
    }


def _parse_ras_mc_errors(text: str) -> list[dict]:
    dimms = []
    for line in text.splitlines():
        m = re.search(r"(mc\d+/csrow\d+/ch\d+|DIMM_\S+)\s+(\d+)\s+CE\s+(\d+)\s+UE", line, re.IGNORECASE)
        if m:
            dimms.append({
                "dimm": m.group(1),
                "ce_count": int(m.group(2)),
                "ue_count": int(m.group(3)),
            })
    return dimms


def _edac_sysfs(node: str) -> dict[str, Any]:
    """Read EDAC counters directly from /sys/bus/edac/devices/ as fallback."""
    rc, out, err = _run(
        ["find", "/sys/bus/edac/devices/", "-name", "ue_count", "-o", "-name", "ce_count"],
        node,
    )
    if rc != 0:
        return {"error": "edac_not_available",
                "detail": "ras-mc-ctl, edac-util, and /sys/bus/edac not available; "
                          "EDAC kernel modules may not be loaded"}
    return {"summary": {"raw_sysfs_paths": out.strip().splitlines()},
            "details_by_dimm": [], "trend": "unknown"}


# ── get_ipmi_sel ──────────────────────────────────────────────────────────────


def get_ipmi_sel(
    node: str = "local",
    since: str | None = None,
    severity_filter: str = "all",
) -> dict[str, Any]:
    """Read IPMI System Event Log via ipmitool."""
    rc, out, err = _run(["ipmitool", "sel", "elist"], node)
    if rc == -1:
        return {"error": "ipmitool_not_installed", "detail": err}
    if rc != 0:
        if "could not open" in err.lower() or "unable to establish" in err.lower():
            return {"error": "ipmi_credentials_missing",
                    "detail": "Cannot connect to BMC; check IPMI credentials or use -I lan"}
        return {"error": "ipmitool_error", "detail": err[:300]}

    events = _parse_ipmi_sel(out)
    if severity_filter == "critical_only":
        events = [e for e in events if e.get("severity") in ("Critical", "Warning")]

    interpretation = _interpret_ipmi_events(events)
    bmc_skew = _get_bmc_clock_skew(node)

    return {
        "events": events[:50],  # cap to avoid overwhelming LLM context
        "interpretation": interpretation,
        "bmc_clock_skew_seconds": bmc_skew,
    }


def _parse_ipmi_sel(text: str) -> list[dict]:
    """Parse ipmitool sel elist output into structured events.

    ipmitool sel elist format:
      ID | timestamp | sensor_name | event_description | assertion
    """
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        # Join parts[3:] as full event description (may contain | in event text)
        event_text = " | ".join(parts[3:]).strip() if len(parts) > 3 else ""
        full_text = (event_text + " " + " ".join(parts)).lower()
        severity = "Critical" if any(
            w in full_text
            for w in ("uncorrectable", "uncorrected", "critical", "failure", "fatal")
        ) else "Warning" if any(
            w in full_text for w in ("correctable", "corrected", "warning", "threshold")
        ) else "Info"

        events.append({
            "id": parts[0],
            "timestamp": parts[1],
            "sensor": parts[2] if len(parts) > 2 else "",
            "event": event_text,
            "severity": severity,
        })
    return events


def _interpret_ipmi_events(events: list[dict]) -> str:
    critical = [e for e in events if e.get("severity") == "Critical"]
    if not critical:
        return "No critical IPMI events found."
    mem_events = [e for e in critical if "mem" in e.get("sensor", "").lower()
                  or "dimm" in e.get("sensor", "").lower()
                  or "ecc" in e.get("event", "").lower()]
    if mem_events:
        return (
            f"{len(mem_events)} critical memory event(s) found in IPMI SEL; "
            "strong hardware correlation — likely DIMM failure"
        )
    return f"{len(critical)} critical IPMI event(s); review timestamps against kernel panic"


def _get_bmc_clock_skew(node: str) -> int | None:
    """Estimate BMC clock skew vs OS clock (seconds)."""
    rc, out, _ = _run(["ipmitool", "sel", "time", "get"], node)
    if rc != 0:
        return None
    m = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})", out)
    if not m:
        return None
    try:
        bmc_time = datetime.strptime(m.group(1), "%m/%d/%Y %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        skew = int(abs((datetime.now(timezone.utc) - bmc_time).total_seconds()))
        return skew if skew < 86400 else None  # ignore if >1 day (likely parse error)
    except ValueError:
        return None


# ── get_hardware_inventory ────────────────────────────────────────────────────


def get_hardware_inventory(node: str = "local") -> dict[str, Any]:
    """Retrieve hardware inventory and firmware versions via dmidecode."""
    rc, out, err = _run(
        ["dmidecode", "-t", "system,bios,processor,memory,baseboard"],
        node,
    )
    if rc == -1:
        return {"error": "dmidecode_not_installed", "detail": err}
    if rc != 0:
        return {"error": "dmidecode_failed",
                "detail": err[:300] + " (may require root / sudo)"}

    return _parse_dmidecode(out)


def _parse_dmidecode(text: str) -> dict[str, Any]:
    """Parse dmidecode output into structured inventory."""
    system: dict[str, str] = {}
    bios: dict[str, str] = {}
    cpu_list: list[dict] = []
    dimm_list: list[dict] = []
    current_section = ""
    current_obj: dict[str, str] = {}

    def _flush():
        nonlocal current_obj
        if current_section == "System Information" and current_obj:
            system.update(current_obj)
        elif current_section == "BIOS Information" and current_obj:
            bios.update(current_obj)
        elif current_section == "Processor Information" and current_obj:
            cpu_list.append(dict(current_obj))
        elif current_section in ("Memory Device", "Memory Module") and current_obj:
            dimm_list.append(dict(current_obj))
        current_obj = {}

    for line in text.splitlines():
        if line.startswith("Handle"):
            _flush()
            current_section = ""
        elif not line.startswith("\t") and line.strip():
            _flush()
            current_section = line.strip()
        elif ":" in line:
            k, _, v = line.strip().partition(":")
            current_obj[k.strip()] = v.strip()

    _flush()

    return {
        "system": {
            "vendor": system.get("Manufacturer", "unknown"),
            "product": system.get("Product Name", "unknown"),
            "serial": system.get("Serial Number", "unknown"),
        },
        "bios": {
            "vendor": bios.get("Vendor", "unknown"),
            "version": bios.get("Version", "unknown"),
            "release_date": bios.get("Release Date", "unknown"),
        },
        "cpu": [
            {
                "model": c.get("Version", c.get("Socket Designation", "unknown")),
                "cores": c.get("Core Count", "unknown"),
                "speed_mhz": c.get("Current Speed", "unknown"),
            }
            for c in cpu_list
            if c.get("Status", "").lower() not in ("not present", "disabled")
        ],
        "memory": {
            "dimms": [
                {
                    "slot": d.get("Locator", d.get("Bank Locator", "unknown")),
                    "size": d.get("Size", "unknown"),
                    "manufacturer": d.get("Manufacturer", "unknown"),
                    "speed": d.get("Speed", "unknown"),
                    "part_number": d.get("Part Number", "unknown"),
                }
                for d in dimm_list
                if d.get("Size", "").strip() not in ("", "No Module Installed", "Not Installed")
            ],
            "total_slots": len(dimm_list),
        },
    }
