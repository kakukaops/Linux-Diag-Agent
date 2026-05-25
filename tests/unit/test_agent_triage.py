"""Unit tests for agent triage nodes (WBS 7.1; v2 T-001/T-002)."""

import pytest
from agent.triage.nodes import (
    parse_input,
    classify_fault_and_route,
    detect_taint_and_hw_signals,
    _select_route,
    _implies_recent_change,
)
from mcp_servers.shared.taint_flags import extract_taint_letters as _extract_taint_letters
from agent.triage.state import TriageState


DMESG_TEXT = """\
[  100.000000] BUG: KASAN: use-after-free in tcp_v4_do_rcv+0x123/0x456
[  100.000001] Call Trace:
[  100.000002]  tcp_v4_do_rcv+0x123/0x456
"""

OOM_TEXT = """\
[  200.000000] Out of memory: Kill process 999 (oom_victim) score 800 or sacrifice child
[  200.000001] Killed process 999 (oom_victim) total-vm:102400kB, anon-rss:80000kB
"""

HW_ERROR_DMESG = """\
[ 1234.5] mce: [Hardware Error]: Machine check events logged
[ 1234.6] CPU 2 Bank 2: ee2000000003110a
[ 1234.7] EDAC MC0: 1 UE memory read error on DIMM_A2
"""


# ── parse_input ─────────────────────────────────────────────────────────────

def test_parse_input_detects_dmesg():
    result = parse_input({"raw_input": DMESG_TEXT})
    assert result["input_type"] == "dmesg"


def test_parse_input_detects_question():
    result = parse_input({"raw_input": "How do I diagnose OOM on OLK-6.6?"})
    assert result["input_type"] == "question"


# ── classify_fault_and_route (T-002) ────────────────────────────────────────

def test_classify_fault_from_events():
    state: TriageState = {
        "raw_input": OOM_TEXT,
        "kernel_events": [{"kind": "oom", "summary": "OOM kill: pid=999",
                           "raw": OOM_TEXT, "call_trace": [], "metadata": {}}],
        "fault_summary": "",
    }
    result = classify_fault_and_route(state)
    assert result["fault_kind"] == "oom"
    assert result["sop_name"] == "oom"
    assert result["diagnostic_route"] == "kernel"


def test_classify_fault_panic_priority():
    state: TriageState = {
        "raw_input": "",
        "kernel_events": [
            {"kind": "oom", "summary": "OOM", "raw": "", "call_trace": [], "metadata": {}},
            {"kind": "panic", "summary": "Kernel panic", "raw": "", "call_trace": [], "metadata": {}},
        ],
        "fault_summary": "",
    }
    result = classify_fault_and_route(state)
    assert result["fault_kind"] == "panic"
    assert result["sop_name"] == "panic"
    assert result["diagnostic_route"] == "kernel"   # no vmcore, no hw signal


def test_classify_routes_hardware_when_hw_signal():
    state: TriageState = {
        "raw_input": "",
        "kernel_events": [{"kind": "hardlockup", "summary": "hard lockup",
                           "raw": "", "call_trace": [], "metadata": {}}],
        "has_hardware_signal": True,   # set by detect_taint_and_hw_signals
        "fault_summary": "",
    }
    result = classify_fault_and_route(state)
    assert result["fault_kind"] == "hardlockup"
    assert result["diagnostic_route"] == "hardware"   # NOT lockup→kernel
    assert result["sop_name"] == "hardware"


# ── detect_taint_and_hw_signals (T-001) ─────────────────────────────────────

def test_detect_hw_signals_from_mce_dmesg():
    result = detect_taint_and_hw_signals({"dmesg_text": HW_ERROR_DMESG})
    assert result["has_hardware_signal"] is True
    assert "hardware_error" in result["hardware_signals"]
    assert "mce" in result["hardware_signals"]
    assert "edac_uncorrected" in result["hardware_signals"]


def test_detect_no_hw_signals_on_clean_dmesg():
    result = detect_taint_and_hw_signals({"dmesg_text": DMESG_TEXT})
    assert result["has_hardware_signal"] is False
    assert result["hardware_signals"] == []


def test_detect_taint_M_triggers_hw_signal():
    state = {"dmesg_text": "CPU: 1 PID: 2 Comm: x Tainted: G   M      6.6.0 #1"}
    result = detect_taint_and_hw_signals(state)
    assert "M" in result["taint_flags"]
    assert "taint_machine_check" in result["hardware_signals"]


def test_extract_taint_letters():
    assert _extract_taint_letters("Tainted: G        W          6.6.0 #1") == ["G", "W"]
    assert _extract_taint_letters("no taint line here") == []


# ── _select_route ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,fault_kind,has_events,expected", [
    ({"has_hardware_signal": True}, "hardlockup", True, "hardware"),
    ({"vmcore_path": "/var/crash/vmcore"}, "panic", True, "kernel+vmcore"),
    ({}, "panic", True, "kernel"),                       # panic but no vmcore
    ({"raw_input": "升级内核后频繁 OOM"}, "oom", True, "change"),
    ({"raw_input": "kernel worked fine yesterday"}, "oom", True, "change"),
    ({}, "generic", False, "unknown"),                   # vague question
    ({}, "oom", True, "kernel"),                         # default
])
def test_select_route(state, fault_kind, has_events, expected):
    assert _select_route(state, fault_kind, has_events=has_events) == expected


def test_implies_recent_change():
    assert _implies_recent_change({"raw_input": "升级内核后崩溃"})
    assert _implies_recent_change({"raw_input": "after the upgrade it panics"})
    assert not _implies_recent_change({"raw_input": "How to diagnose an OOM kill?"})
