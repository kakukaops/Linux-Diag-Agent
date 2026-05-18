"""Unit tests for dmesg/journal event extractor (WBS 4.5)."""

import pytest
from mcp_servers.dmesg_journal.extractor import extract_events, EventKind


OOPS_SAMPLE = """\
[  123.456789] BUG: KASAN: use-after-free in tcp_v4_do_rcv+0x123/0x456
[  123.456790] Read of size 8 at addr ffff888012345678 by task swapper/0/1
[  123.456791] Call Trace:
[  123.456792]  dump_stack_lvl+0x45/0x5a
[  123.456793]  tcp_v4_do_rcv+0x123/0x456
"""

OOM_SAMPLE = """\
[  200.000000] Out of memory: Kill process 1234 (oom_test) score 999 or sacrifice child
[  200.000001] Killed process 1234 (oom_test) total-vm:1000kB, anon-rss:800kB
"""

SOFTLOCKUP_SAMPLE = """\
[  500.000000] watchdog: BUG: soft lockup - CPU#3 stuck for 22s! [kworker/3:1H:42]
[  500.000001] Call Trace:
[  500.000002]  __schedule+0x3a5/0xb20
"""

PANIC_SAMPLE = """\
[    1.234567] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
[    1.234568] Call Trace:
[    1.234569]  panic+0x14b/0x330
"""


def test_oops_detected():
    events = extract_events(OOPS_SAMPLE)
    assert len(events) >= 1
    kinds = {e.kind for e in events}
    assert EventKind.bug in kinds or EventKind.oops in kinds


def test_oom_detected():
    events = extract_events(OOM_SAMPLE)
    assert any(e.kind == EventKind.oom for e in events)
    oom = next(e for e in events if e.kind == EventKind.oom)
    assert oom.metadata["pid"] == "1234"
    assert oom.metadata["comm"] == "oom_test"


def test_softlockup_detected():
    events = extract_events(SOFTLOCKUP_SAMPLE)
    assert any(e.kind == EventKind.softlockup for e in events)


def test_panic_detected():
    events = extract_events(PANIC_SAMPLE)
    assert any(e.kind == EventKind.panic for e in events)


def test_empty_log():
    events = extract_events("")
    assert events == []


def test_no_events_in_normal_log():
    log = "[  1.000000] Linux version 5.10.0\n[  1.000001] Command line: ro quiet\n"
    events = extract_events(log)
    assert events == []


def test_event_to_dict():
    events = extract_events(OOM_SAMPLE)
    oom = next(e for e in events if e.kind == EventKind.oom)
    d = oom.to_dict()
    assert d["kind"] == "oom"
    assert "summary" in d
    assert "raw" in d
