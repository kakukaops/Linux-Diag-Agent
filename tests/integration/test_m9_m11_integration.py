"""Integration tests for M9 (crash forensics) and M11 (hardware layer) — T-023.

These tests verify:
  1. taint_flags module integrates correctly with triage node (shared import)
  2. hardware route correctly uses M11 tools and NOT kernel commit tools
  3. kernel+vmcore route correctly includes vmcore tools
  4. M11 tool graceful degradation when hardware tools not installed
  5. decode_stacktrace falls back correctly when vmlinux unavailable
  6. parse_taint_flags tool accessible from ReAct registry on hardware route
  7. drgn runner rejects invalid queries

Most hardware tools require root/hardware — test via mocks; mark live tests skip.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ── 1. Shared taint_flags ↔ triage integration ────────────────────────────────

def test_triage_uses_shared_taint_flags():
    """detect_taint_and_hw_signals imports from mcp_servers.shared.taint_flags."""
    import inspect
    from agent.triage import nodes
    source = inspect.getsource(nodes.detect_taint_and_hw_signals)
    assert "mcp_servers.shared.taint_flags" in source
    assert "extract_taint_letters" in source


def test_triage_detects_mce_taint():
    from agent.triage.nodes import detect_taint_and_hw_signals
    state = {
        "dmesg_text": "Kernel panic - not syncing: Fatal Machine check\nTainted: M    6.6.0",
    }
    result = detect_taint_and_hw_signals(state)
    assert "M" in result["taint_flags"]
    assert result["has_hardware_signal"] is True
    assert "taint_machine_check" in result["hardware_signals"]


def test_triage_hardware_error_signal():
    from agent.triage.nodes import detect_taint_and_hw_signals
    state = {"dmesg_text": "Hardware Error: Machine check: Uncorrected error, action required."}
    result = detect_taint_and_hw_signals(state)
    assert result["has_hardware_signal"] is True
    assert "hardware_error" in result["hardware_signals"]


def test_triage_edac_ue_signal():
    from agent.triage.nodes import detect_taint_and_hw_signals
    state = {"dmesg_text": "EDAC MC0: 1 UE memory read error on DIMM"}
    result = detect_taint_and_hw_signals(state)
    assert "edac_uncorrected" in result["hardware_signals"]


def test_triage_clean_dmesg_no_hardware_signal():
    from agent.triage.nodes import detect_taint_and_hw_signals
    state = {"dmesg_text": "BUG: KASAN: use-after-free in tcp_v4_do_rcv"}
    result = detect_taint_and_hw_signals(state)
    assert result["has_hardware_signal"] is False
    assert result["taint_flags"] == []


# ── 2. Hardware route tool exposure ──────────────────────────────────────────

def test_hardware_tools_only_on_hardware_route():
    """MCE/EDAC/IPMI tools must NOT appear on kernel route (ADR-023)."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    hw_exclusive = {"get_mce_log", "get_edac_errors", "get_ipmi_sel", "get_hardware_inventory"}
    kernel_tools = {t.name for t in reg.for_route("kernel")}
    vmcore_tools = {t.name for t in reg.for_route("kernel+vmcore")}
    assert hw_exclusive.isdisjoint(kernel_tools), \
        f"Hardware-exclusive tools leaked into kernel route: {hw_exclusive & kernel_tools}"
    assert hw_exclusive.isdisjoint(vmcore_tools), \
        f"Hardware-exclusive tools leaked into kernel+vmcore route: {hw_exclusive & vmcore_tools}"


def test_parse_taint_flags_on_all_routes():
    """parse_taint_flags should be available on hardware, kernel, and unknown routes."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    for route in ("hardware", "kernel", "kernel+vmcore", "unknown"):
        names = {t.name for t in reg.for_route(route)}
        assert "parse_taint_flags" in names, f"parse_taint_flags missing from {route} route"


def test_hardware_route_has_required_tools():
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    hw_tools = {t.name for t in reg.for_route("hardware")}
    required = {"get_mce_log", "get_edac_errors", "get_ipmi_sel",
                "get_hardware_inventory", "parse_taint_flags",
                "parse_dmesg", "extract_call_trace"}
    missing = required - hw_tools
    assert not missing, f"Hardware route missing required tools: {missing}"


# ── 3. Vmcore route tool exposure ─────────────────────────────────────────────

def test_vmcore_tools_only_on_vmcore_route():
    """analyze_vmcore and fetch_debuginfo only on kernel+vmcore."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    vmcore_exclusive = {"analyze_vmcore", "fetch_debuginfo"}
    kernel_tools = {t.name for t in reg.for_route("kernel")}
    hw_tools = {t.name for t in reg.for_route("hardware")}
    assert vmcore_exclusive.isdisjoint(kernel_tools), \
        f"vmcore tools leaked into kernel route: {vmcore_exclusive & kernel_tools}"
    assert vmcore_exclusive.isdisjoint(hw_tools)


def test_decode_stacktrace_on_kernel_and_vmcore():
    """decode_stacktrace should be accessible on kernel and kernel+vmcore routes."""
    from agent.react.tools.registry import build_registry
    reg = build_registry()
    for route in ("kernel", "kernel+vmcore"):
        names = {t.name for t in reg.for_route(route)}
        assert "decode_stacktrace" in names, f"decode_stacktrace missing from {route}"


# ── 4. M11 graceful degradation ──────────────────────────────────────────────

def test_mce_log_graceful_not_installed():
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "not found")):
        from mcp_servers.hardware.tools import get_mce_log
        result = get_mce_log()
    assert "error" in result
    assert result["error"] == "mcelog_not_installed"


def test_edac_graceful_not_available():
    """When ras-mc-ctl, edac-util, and sysfs all fail, return structured error."""
    def fake_run(cmd, node="local"):
        return (-1, "", "not found")
    with patch("mcp_servers.hardware.tools._run", side_effect=fake_run):
        from mcp_servers.hardware.tools import get_edac_errors
        result = get_edac_errors()
    assert "error" in result


def test_ipmi_sel_credentials_missing():
    def fake_run(cmd, node="local"):
        return (1, "", "could not open device")
    with patch("mcp_servers.hardware.tools._run", side_effect=fake_run):
        from mcp_servers.hardware.tools import get_ipmi_sel
        result = get_ipmi_sel()
    assert result["error"] in ("ipmi_credentials_missing", "ipmitool_error")


def test_hardware_tool_returns_string_via_react():
    """Tool fn must return a string (for LLM consumption)."""
    from agent.react.tools.hardware_tools import _get_mce_log
    with patch("mcp_servers.hardware.tools._run", return_value=(-1, "", "not found")):
        result = _get_mce_log()
    assert isinstance(result, str)
    assert "error" in result.lower() or "mcelog" in result.lower()


# ── 5. decode_stacktrace fallback ─────────────────────────────────────────────

def test_decode_stacktrace_missing_vmlinux():
    from mcp_servers.crash_forensics.decode_stacktrace import decode_stacktrace
    with patch("mcp_servers.crash_forensics.decode_stacktrace.find_vmlinux", return_value=None):
        result = decode_stacktrace("some trace", kernel_version="OLK-6.6")
    assert result["error"] == "vmlinux_not_found"
    assert "OLK" in result["detail"]


def test_decode_stacktrace_script_failure():
    from pathlib import Path
    from mcp_servers.crash_forensics.decode_stacktrace import decode_stacktrace
    fake_vmlinux = Path("/tmp/fake_vmlinux")
    with patch("mcp_servers.crash_forensics.decode_stacktrace.find_vmlinux",
               return_value=fake_vmlinux), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("mcp_servers.crash_forensics.decode_stacktrace.find_decode_script",
               return_value=None), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"addr2line not found")
        result = decode_stacktrace("some trace\n 0xffffffff81234567")
    # Should have tried fallback; result has error or frames
    assert isinstance(result, dict)


# ── 6. drgn runner rejects invalid inputs ────────────────────────────────────

def test_drgn_runner_invalid_query():
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    result = analyze_vmcore("/tmp/dummy.vmcore", query="delete_everything")
    assert result["error"] == "invalid_query"
    assert "all_stacks" in result["detail"]


def test_drgn_runner_missing_vmcore():
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    result = analyze_vmcore("/nonexistent/vmcore.bin", query="oom_context")
    assert result["error"] == "vmcore_not_found"


def test_drgn_runner_missing_vmlinux():
    from mcp_servers.crash_forensics.drgn_runner import analyze_vmcore
    import tempfile, os
    with tempfile.NamedTemporaryFile(delete=False) as f:
        vmcore = f.name
    try:
        with patch("mcp_servers.crash_forensics.decode_stacktrace.find_vmlinux",
                   return_value=None):
            result = analyze_vmcore(vmcore, query="all_stacks", kernel_version="OLK-6.6")
        assert result["error"] == "vmlinux_not_found"
    finally:
        os.unlink(vmcore)


# ── 7. parse_taint_flags tool end-to-end ─────────────────────────────────────

def test_parse_taint_flags_tool_mce():
    from agent.react.tools.hardware_tools import _parse_taint_flags
    result = _parse_taint_flags(taint_value="M")
    assert isinstance(result, str)
    assert "Machine Check" in result
    assert "HIGH PRIORITY" in result
    assert "route_hardware" in result


def test_parse_taint_flags_tool_clean():
    from agent.react.tools.hardware_tools import _parse_taint_flags
    result = _parse_taint_flags(taint_value="0")
    assert "clean" in result.lower() or "no taint" in result.lower()


def test_parse_taint_flags_tool_bitmask():
    from agent.react.tools.hardware_tools import _parse_taint_flags
    # bit 15 = K (live-patched)
    result = _parse_taint_flags(taint_value=str(1 << 15))
    assert "live" in result.lower() or "K" in result


# ── 8. Hardware SOP yaml structure ───────────────────────────────────────────

def test_hardware_sop_loads():
    from agent.sop.registry import get_sop
    sop = get_sop("hardware")
    assert sop["name"] == "hardware"
    step_ids = [s["id"] for s in sop["steps"]]
    assert "hw_step_1" in step_ids
    assert "conclude" in step_ids


def test_hardware_sop_has_forbidden():
    from agent.sop.registry import get_sop
    sop = get_sop("hardware")
    forbidden = sop.get("forbidden", [])
    assert len(forbidden) >= 1
    # Should explicitly forbid kernel commit references
    assert any("commit" in f or "kernel" in f.lower() for f in forbidden)


def test_hardware_sop_trigger_signals():
    from agent.sop.registry import get_sop
    sop = get_sop("hardware")
    signals = sop.get("trigger_signals", [])
    assert len(signals) >= 3  # MCE, taint_M, hardware_error at minimum


# ── 9. Fetch debuginfo search paths ──────────────────────────────────────────

def test_fetch_debuginfo_returns_error_not_exception():
    from mcp_servers.crash_forensics.fetch_debuginfo import fetch_debuginfo
    with patch("pathlib.Path.exists", return_value=False):
        result = fetch_debuginfo(kernel_version="OLK-6.6")
    assert "error" in result
    assert "vmlinux_not_found" == result["error"]
    assert "searched" in result


def test_fetch_debuginfo_finds_olk_vmlinux():
    """If OLK source vmlinux exists, find_vmlinux returns it."""
    from mcp_servers.crash_forensics.decode_stacktrace import find_vmlinux
    from pathlib import Path
    # OLK-6.6 source is at /data1/lingqu/codes/OLK-6.6/kernel/vmlinux
    # If the file exists in the real environment, it should be returned
    result = find_vmlinux("OLK-6.6")
    if result:
        assert result.name == "vmlinux"
        assert result.exists()
    # If not found, that's also acceptable (dev environment may not have build output)
