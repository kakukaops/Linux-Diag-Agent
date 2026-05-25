"""decode_stacktrace wrapper (T-015).

Wraps `scripts/decode_stacktrace.sh vmlinux < raw_trace` from the Linux kernel
source tree. vmlinux must be provided; it can come from fetch_debuginfo (T-014)
or be specified directly for development.

OLK source locations (from CLAUDE.md):
  OLK-6.6: /data1/lingqu/codes/OLK-6.6/kernel
  OLK-5.10: /data1/lingqu/codes/OLK-5.10/kernel
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


_TIMEOUT = 60  # decode can be slow on large traces

# OLK kernel source paths (ADR-018)
_OLK_SOURCES: dict[str, Path] = {
    "OLK-6.6": Path("/data1/lingqu/codes/OLK-6.6/kernel"),
    "OLK-5.10": Path("/data1/lingqu/codes/OLK-5.10/kernel"),
}

# decode_stacktrace.sh location relative to kernel source root
_DECODE_SCRIPT = "scripts/decode_stacktrace.sh"


def find_vmlinux(kernel_version: str | None = None) -> Path | None:
    """Find vmlinux from OLK source dir or /boot for the given kernel version."""
    # 1. OLK source dir has vmlinux at root after build
    if kernel_version:
        for tag, src in _OLK_SOURCES.items():
            if kernel_version in tag or tag in (kernel_version or ""):
                candidate = src / "vmlinux"
                if candidate.exists():
                    return candidate

    # 2. Try all OLK source dirs
    for src in _OLK_SOURCES.values():
        candidate = src / "vmlinux"
        if candidate.exists():
            return candidate

    # 3. /boot/vmlinux-<uname>
    boot = Path("/boot")
    if boot.exists():
        candidates = sorted(boot.glob("vmlinux-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]

    return None


def find_decode_script(kernel_version: str | None = None) -> Path | None:
    """Locate decode_stacktrace.sh from the OLK kernel source."""
    if kernel_version:
        for tag, src in _OLK_SOURCES.items():
            if kernel_version in tag or tag in (kernel_version or ""):
                script = src / _DECODE_SCRIPT
                if script.exists():
                    return script

    for src in _OLK_SOURCES.values():
        script = src / _DECODE_SCRIPT
        if script.exists():
            return script

    return None


def decode_stacktrace(
    raw_trace: str,
    kernel_version: str | None = None,
    vmlinux_path: str | None = None,
) -> dict[str, Any]:
    """Translate a raw kernel stack trace to function+file:line.

    Args:
        raw_trace: multi-line dmesg stack trace (lines with hex addresses).
        kernel_version: e.g. "OLK-6.6" or "OLK-5.10"; used to locate vmlinux.
        vmlinux_path: explicit path to vmlinux; overrides auto-detection.

    Returns:
        dict with 'frames' list (address, function, file, line, offset)
        or 'error' key if decode failed.
    """
    # Resolve vmlinux
    vmlinux = Path(vmlinux_path) if vmlinux_path else find_vmlinux(kernel_version)
    if not vmlinux or not vmlinux.exists():
        return {
            "error": "vmlinux_not_found",
            "detail": (
                f"Cannot locate vmlinux for kernel_version={kernel_version!r}. "
                "Provide vmlinux_path explicitly, or run fetch_debuginfo first. "
                f"Checked OLK source dirs: {list(_OLK_SOURCES.values())}"
            ),
        }

    # Find decode_stacktrace.sh
    script = find_decode_script(kernel_version)
    if not script:
        # Fall back to addr2line if script not found
        return _fallback_addr2line(raw_trace, vmlinux)

    # Run decode_stacktrace.sh
    try:
        proc = subprocess.run(
            ["bash", str(script), str(vmlinux)],
            input=raw_trace.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": "decode_timeout", "detail": f"decode_stacktrace.sh timed out after {_TIMEOUT}s"}
    except Exception as exc:
        return {"error": "decode_failed", "detail": str(exc)}

    if proc.returncode != 0:
        return {
            "error": "decode_script_failed",
            "detail": proc.stderr.decode("utf-8", errors="replace")[:500],
        }

    output = proc.stdout.decode("utf-8", errors="replace")
    frames = _parse_decoded_output(output)
    return {"frames": frames, "vmlinux_used": str(vmlinux)}


def _parse_decoded_output(text: str) -> list[dict[str, Any]]:
    """Parse decode_stacktrace.sh output into structured frames."""
    frames = []
    for line in text.splitlines():
        # Typical output: " [<ffffffff81234>] do_mmap+0x123/0x456 mm/mmap.c:1521"
        m = re.search(
            r"([0-9a-fA-F]{8,16})\s+"
            r"(\S+)\+([0-9a-fx/]+)\s+"
            r"(\S+):(\d+)",
            line,
        )
        if m:
            frames.append({
                "address": m.group(1),
                "function": m.group(2),
                "offset": m.group(3),
                "file": m.group(4),
                "line": int(m.group(5)),
            })
            continue

        # Fallback: capture lines that look like decoded frames
        m2 = re.search(r"(\S+)\+([0-9a-fx/]+)\s+\[(\S+)\]", line)
        if m2:
            frames.append({
                "address": "",
                "function": m2.group(1),
                "offset": m2.group(2),
                "file": m2.group(3),
                "line": 0,
            })

    return frames


def _fallback_addr2line(raw_trace: str, vmlinux: Path) -> dict[str, Any]:
    """addr2line-based fallback when decode_stacktrace.sh is unavailable."""
    # Extract hex addresses from the trace
    addrs = re.findall(r"\b([0-9a-fA-F]{8,16})\b", raw_trace)
    if not addrs:
        return {"error": "no_addresses_found", "detail": "No hex addresses in trace"}

    try:
        proc = subprocess.run(
            ["addr2line", "-f", "-e", str(vmlinux)] + addrs[:50],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {"error": "addr2line_not_found",
                "detail": "Neither decode_stacktrace.sh nor addr2line is available"}
    except Exception as exc:
        return {"error": "addr2line_failed", "detail": str(exc)}

    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    frames = []
    for i in range(0, len(lines) - 1, 2):
        func = lines[i].strip()
        loc = lines[i + 1].strip() if i + 1 < len(lines) else ""
        file_part, _, line_part = loc.partition(":")
        frames.append({
            "address": addrs[i // 2] if i // 2 < len(addrs) else "",
            "function": func if func != "??" else "unknown",
            "offset": "",
            "file": file_part if file_part != "??" else "unknown",
            "line": int(line_part) if line_part.isdigit() else 0,
        })
    return {"frames": frames, "vmlinux_used": str(vmlinux), "method": "addr2line"}
