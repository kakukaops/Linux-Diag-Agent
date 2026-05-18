"""Unit tests for agent triage nodes (WBS 7.1)."""

import pytest
from agent.triage.nodes import parse_input, classify_fault
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


def test_parse_input_detects_dmesg():
    state: TriageState = {"raw_input": DMESG_TEXT}
    result = parse_input(state)
    assert result["input_type"] == "dmesg"


def test_parse_input_detects_question():
    state: TriageState = {"raw_input": "How do I diagnose OOM on OLK-6.6?"}
    result = parse_input(state)
    assert result["input_type"] == "question"


def test_classify_fault_from_events():
    state: TriageState = {
        "raw_input": OOM_TEXT,
        "kernel_events": [{"kind": "oom", "summary": "OOM kill: pid=999", "raw": OOM_TEXT, "call_trace": [], "metadata": {}}],
        "fault_summary": "",
    }
    result = classify_fault(state)
    assert result["fault_kind"] == "oom"
    assert result["sop_name"] == "oom"


def test_classify_fault_panic_priority():
    state: TriageState = {
        "raw_input": "",
        "kernel_events": [
            {"kind": "oom", "summary": "OOM", "raw": "", "call_trace": [], "metadata": {}},
            {"kind": "panic", "summary": "Kernel panic", "raw": "", "call_trace": [], "metadata": {}},
        ],
        "fault_summary": "",
    }
    result = classify_fault(state)
    assert result["fault_kind"] == "panic"
    assert result["sop_name"] == "panic"
