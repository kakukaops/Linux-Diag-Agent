"""Unit tests for M14: taint_flags, hardware tools, vmcore tools."""

import pytest
from unittest.mock import patch, MagicMock


# ── T-016: taint_flags ────────────────────────────────────────────────────────

from mcp_servers.shared.taint_flags import (
    decode_taint, extract_taint_letters, TAINT_TABLE
)


def test_extract_taint_letters_basic():
    text = "Kernel panic - not syncing: Tainted: G M W    "
    letters = extract_taint_letters(text)
    assert "G" in letters
    assert "M" in letters
    assert "W" in letters


def test_extract_taint_letters_no_taint():
    assert extract_taint_letters("plain dmesg text") == []


def test_decode_taint_letters():
    result = decode_taint("M B")
    assert result["high_priority_flags"] == ["M", "B"]
    assert any(f["letter"] == "M" for f in result["flags"])
    assert any(f["letter"] == "B" for f in result["flags"])
    assert "route_hardware" in result["search_narrowing_hints"]


def test_decode_taint_integer_bitmask():
    # bit 4 = M (machine check), bit 0 = G (proprietary)
    val = (1 << 4) | (1 << 0)
    result = decode_taint(str(val))
    letters = {f["letter"] for f in result["flags"]}
    assert "M" in letters
    assert "G" in letters
    assert "M" in result["high_priority_flags"]


def test_decode_taint_integer_4096():
    # 4096 = 0x1000 = bit 12 = 'O' (out-of-tree module)
    result = decode_taint("4096")
    assert result["flags"][0]["letter"] == "O"


def test_decode_taint_clean():
    # no flags
    result = decode_taint("0")
    assert result["flags"] == []
    assert result["high_priority_flags"] == []


def test_decode_taint_no_spaces():
    result = decode_taint("GMW")
    letters = {f["letter"] for f in result["flags"]}
    assert "G" in letters
    assert "M" in letters
    assert "W" in letters


def test_taint_table_complete():
    # All standard Linux taint letters 0-17 should be in table
    expected_letters = set("GFSR MBUADWCIOELIKE LKT") - {" "}
    table_letters = {f.letter for f in TAINT_TABLE}
    for letter in "MBOILKE":  # spot-check key ones
        assert letter in table_letters, f"Missing taint flag {letter}"


# ── T-019: hardware tools subprocess wrappers ─────────────────────────────────

from mcp_servers.hardware.tools import (
    get_mce_log, get_edac_errors, get_ipmi_sel, get_hardware_inventory,
    _parse_mcelog_output, _parse_dmidecode, _parse_ipmi_sel,
)


def test_get_mce_log_not_installed():
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "command not found")):
        result = get_mce_log()
    assert result["error"] == "mcelog_not_installed"


def test_get_mce_log_not_running_fallback_fails():
    def fake_run(cmd, node="local"):
        if "--client" in cmd:
            return (1, "", "not running")
        return (1, "", "no such file")
    with patch("mcp_servers.hardware.tools._run", side_effect=fake_run):
        result = get_mce_log()
    assert result["error"] == "mcelog_not_running"


def test_parse_mcelog_empty():
    events = _parse_mcelog_output("")
    assert events == []


def test_parse_mcelog_basic():
    text = """Hardware event. This is not a software error.
CPU 0 BANK 2
TIME 1716134401 Fri May 19 14:33:21 2026
MCi_STATUS 0xff20000080000900
MCi_ADDR 0x0000000100000000
"""
    events = _parse_mcelog_output(text)
    assert len(events) == 1
    assert events[0]["cpu"] == 0
    assert events[0]["bank"] == 2
    assert events[0]["mci_status"] == "0xff20000080000900"


def test_get_edac_not_available():
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "not found")):
        result = get_edac_errors()
    assert "error" in result


def test_get_ipmi_not_installed():
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "not found")):
        result = get_ipmi_sel()
    assert result["error"] == "ipmitool_not_installed"


def test_parse_ipmi_sel_basic():
    text = """  1 | 05/19/2026 14:29:55 | DIMM_A2 | Memory | Uncorrectable ECC | Asserted
  2 | 05/19/2026 14:30:00 | Power Supply | Power Supply Failure | Deasserted
"""
    events = _parse_ipmi_sel(text)
    assert len(events) == 2
    assert events[0]["severity"] == "Critical"
    assert "DIMM_A2" in events[0]["sensor"]


def test_parse_dmidecode_basic():
    text = """System Information
\tManufacturer: Dell Inc.
\tProduct Name: PowerEdge R750
\tSerial Number: ABC123

BIOS Information
\tVendor: Dell Inc.
\tVersion: 1.13.2
\tRelease Date: 03/12/2026

Memory Device
\tLocator: DIMM_A1
\tSize: 32 GB
\tManufacturer: Hynix
\tSpeed: 3200 MT/s
\tPart Number: HMA84GR7AFR4N
"""
    result = _parse_dmidecode(text)
    assert result["system"]["vendor"] == "Dell Inc."
    assert result["bios"]["version"] == "1.13.2"
    assert len(result["memory"]["dimms"]) == 1
    assert result["memory"]["dimms"][0]["slot"] == "DIMM_A1"


def test_get_hardware_inventory_not_installed():
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "command not found")):
        result = get_hardware_inventory()
    assert result["error"] == "dmidecode_not_installed"


# ── MCE codes ─────────────────────────────────────────────────────────────────

from mcp_servers.hardware.mce_codes import decode_mci_status


def test_decode_mci_status_invalid():
    result = decode_mci_status(0)  # VAL bit not set
    assert result["valid"] is False


def test_decode_mci_status_uncorrected():
    # bit 63 (VAL) + bit 61 (UC) + bits 0 = 0x0001
    val = (1 << 63) | (1 << 61) | 0x0001
    result = decode_mci_status(val)
    assert result["valid"] is True
    assert result["uncorrected"] is True
    assert result["severity"] == "uncorrected"


def test_decode_mci_status_fatal():
    # VAL + PCC
    val = (1 << 63) | (1 << 57)
    result = decode_mci_status(val)
    assert result["fatal"] is True
    assert result["severity"] == "fatal"


# ── Hardware Tool objects (ReAct registry) ────────────────────────────────────

from agent.react.tools.hardware_tools import ALL_HARDWARE_TOOLS


def test_hardware_tools_routes():
    hw_names = {t.name for t in ALL_HARDWARE_TOOLS}
    assert "get_mce_log" in hw_names
    assert "get_edac_errors" in hw_names
    assert "get_ipmi_sel" in hw_names
    assert "get_hardware_inventory" in hw_names
    assert "parse_taint_flags" in hw_names


def test_hardware_tools_only_on_hardware_route():
    exclusive_hw = {"get_mce_log", "get_edac_errors", "get_ipmi_sel", "get_hardware_inventory"}
    for t in ALL_HARDWARE_TOOLS:
        if t.name in exclusive_hw:
            assert "hardware" in t.routes
            assert "kernel" not in t.routes, f"{t.name} should NOT appear on kernel route"


# ── Vmcore tools ──────────────────────────────────────────────────────────────

from agent.react.tools.vmcore_tools import ALL_VMCORE_TOOLS


def test_vmcore_tools_on_correct_routes():
    vmcore_names = {t.name for t in ALL_VMCORE_TOOLS}
    assert "analyze_vmcore" in vmcore_names
    assert "decode_stacktrace" in vmcore_names
    assert "fetch_debuginfo" in vmcore_names

    for t in ALL_VMCORE_TOOLS:
        if t.name == "analyze_vmcore":
            assert "kernel+vmcore" in t.routes
            assert "kernel" not in t.routes  # drgn tools only for vmcore route


def test_fetch_debuginfo_vmlinux_not_found():
    """When no vmlinux exists in standard paths, returns error."""
    with patch("mcp_servers.crash_forensics.fetch_debuginfo._OLK_BUILD_DIRS", {}), \
         patch("pathlib.Path.exists", return_value=False):
        from mcp_servers.crash_forensics.fetch_debuginfo import fetch_debuginfo
        result = fetch_debuginfo(kernel_version="OLK-6.6")
    assert "error" in result


def test_decode_stacktrace_vmlinux_not_found():
    from mcp_servers.crash_forensics.decode_stacktrace import decode_stacktrace
    with patch("mcp_servers.crash_forensics.decode_stacktrace.find_vmlinux", return_value=None):
        result = decode_stacktrace("some trace text", kernel_version="OLK-6.6")
    assert result["error"] == "vmlinux_not_found"


def test_analyze_vmcore_invalid_query():
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    result = analyze_vmcore("/tmp/vmcore", query="bad_query")
    assert result["error"] == "invalid_query"


def test_analyze_vmcore_missing_file():
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    result = analyze_vmcore("/nonexistent/vmcore", query="all_stacks")
    assert result["error"] == "vmcore_not_found"


# ── Registry has all tools ────────────────────────────────────────────────────

def test_full_registry_tool_count():
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    assert len(reg.for_route("hardware")) >= 8
    assert len(reg.for_route("kernel+vmcore")) >= 18
    assert len(reg.for_route("kernel")) >= 15
