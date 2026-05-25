"""Shared kernel taint flag bit-mapping table (T-016).

Imported by:
  - mcp_servers/dmesg_journal/: parse_taint_flags MCP tool (M9)
  - mcp_servers/hardware/: M11 hardware layer taint detection
  - agent/triage/nodes.py: detect_taint_and_hw_signals

Ref: Documentation/admin-guide/tainted-kernels.rst (Linux 6.6)
     include/linux/kernel.h TAINT_* constants
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple


class TaintFlag(NamedTuple):
    letter: str
    bit: int
    description: str
    implications: str
    high_priority: bool


TAINT_TABLE: list[TaintFlag] = [
    TaintFlag("G", 0,
              "Proprietary module loaded",
              "A non-GPL module is loaded; some kernel mechanisms may be restricted",
              False),
    TaintFlag("F", 1,
              "Module force-loaded",
              "Module version mismatch — force-loaded despite it; stability uncertain",
              False),
    TaintFlag("S", 2,
              "Out-of-spec hardware",
              "SMP with CPU not certified for SMP, or other hardware spec violation",
              False),
    TaintFlag("R", 3,
              "Module force-unloaded",
              "A module was forcibly removed; kernel state may be inconsistent",
              False),
    TaintFlag("M", 4,
              "Machine Check Exception occurred",
              "Hardware reported an MCE; likely hardware failure — check mcelog/EDAC",
              True),
    TaintFlag("B", 5,
              "Bad page referenced",
              "Kernel accessed a page with unexpected flags; possible memory corruption",
              True),
    TaintFlag("U", 6,
              "Taint requested by userspace",
              "A userspace process explicitly tainted the kernel via /proc/sys/kernel/tainted",
              False),
    TaintFlag("D", 7,
              "Kernel died (prior OOPS/BUG)",
              "A prior OOPS or BUG was triggered; reliability of subsequent traces is uncertain",
              False),
    TaintFlag("A", 8,
              "ACPI table overridden",
              "User ACPI table replaced firmware table; likely BIOS/firmware issue",
              False),
    TaintFlag("W", 9,
              "Kernel warning triggered",
              "A WARN() fired previously; may indicate a non-fatal but real bug",
              False),
    TaintFlag("C", 10,
              "Staging driver loaded",
              "An experimental drivers/staging module is active; lower stability guarantees",
              False),
    TaintFlag("I", 11,
              "Firmware workaround applied",
              "Kernel applied workaround for severe firmware bug; hardware quirk involved",
              False),
    TaintFlag("O", 12,
              "Out-of-tree module loaded",
              "A third-party kernel module not in-tree is loaded",
              False),
    TaintFlag("E", 13,
              "Unsigned module loaded",
              "A module without valid signature is loaded; secure-boot policy weakened",
              False),
    TaintFlag("L", 14,
              "Soft lockup previously occurred",
              "A CPU was stuck >20s without scheduling; look for earlier softlockup dmesg",
              False),
    TaintFlag("K", 15,
              "Kernel live-patched",
              "A live kernel patch (kpatch/livepatch) is active; code paths may differ",
              False),
    TaintFlag("X", 16,
              "Auxiliary distro taint",
              "Distribution-defined taint (OLK: may signal openEuler-specific patch or feature)",
              False),
    TaintFlag("T", 17,
              "Struct layout randomized (RANDSTRUCT)",
              "Kernel built with struct randomization; stack frame layout differs from vanilla",
              False),
]

_BY_LETTER: dict[str, TaintFlag] = {f.letter: f for f in TAINT_TABLE}
_BY_BIT: dict[int, TaintFlag] = {f.bit: f for f in TAINT_TABLE}


def extract_taint_letters(text: str) -> list[str]:
    """Extract kernel taint letters from a dmesg 'Tainted: G W ...' line."""
    m = re.search(r"Tainted:\s*([A-Z ]+)", text)
    if not m:
        return []
    return [c for c in m.group(1) if c.isalpha()]


def decode_taint(taint_value: str) -> dict[str, Any]:
    """Decode taint flags from an integer bitmask or letter string.

    Args:
        taint_value: integer string (e.g. '4096') or letter string
                     (e.g. 'G B C', 'GBC', or from a dmesg 'Tainted:' line).

    Returns:
        flags: list of decoded flag dicts
        high_priority_flags: letters that should trigger hardware/special routing
        search_narrowing_hints: guidance strings for retrieval routing
    """
    letters = _parse_input(taint_value)
    flags = []
    high_priority: list[str] = []

    for letter in letters:
        flag = _BY_LETTER.get(letter)
        if flag:
            flags.append({
                "letter": flag.letter,
                "bit": flag.bit,
                "description": flag.description,
                "implications": flag.implications,
                "high_priority": flag.high_priority,
            })
            if flag.high_priority:
                high_priority.append(flag.letter)
        else:
            # Unknown letter — include with minimal info
            flags.append({
                "letter": letter,
                "bit": None,
                "description": f"Unknown taint flag '{letter}'",
                "implications": "Possibly a vendor/distro-specific taint bit",
                "high_priority": False,
            })

    hints = _build_hints(letters)
    return {
        "flags": flags,
        "high_priority_flags": high_priority,
        "search_narrowing_hints": hints,
    }


def _parse_input(taint_value: str) -> list[str]:
    """Parse taint value: integer bitmask or space/no-delimited letter string."""
    s = taint_value.strip()

    # Pure integer → decode bitmask
    if re.fullmatch(r"\d+", s):
        bits = int(s)
        return [
            flag.letter
            for flag in TAINT_TABLE
            if bits & (1 << flag.bit)
        ]

    # Letter string (with or without spaces) — strip non-alpha chars
    return [c for c in s.upper() if c.isalpha() and c in _BY_LETTER or c.isalpha()]


def _build_hints(letters: list[str]) -> list[str]:
    """Generate retrieval/routing hints from active taint letters."""
    hints: list[str] = []
    if "M" in letters or "B" in letters:
        hints.append("route_hardware")
        hints.append("search_mcelog_edac")
    if "O" in letters or "G" in letters:
        hints.append("include_oot_module_context")
    if "K" in letters:
        hints.append("check_livepatch_interaction")
    if "L" in letters:
        hints.append("search_softlockup_commits")
    if "D" in letters:
        hints.append("prior_oops_check_earlier_dmesg")
    if not hints:
        hints.append("standard_kernel_search")
    return hints
