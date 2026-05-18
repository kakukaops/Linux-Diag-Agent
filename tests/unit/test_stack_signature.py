"""Unit tests for syzbot stack signature."""

import pytest
from ingest.syzbot.signature import stack_signature, normalize_stack


SAMPLE_TRACE = """\
[   12.345678] BUG: KASAN: use-after-free in tcp_v4_do_rcv+0x123/0x456
[   12.345679] Read of size 8 at addr ffff888012345678 by task swapper/0/1
[   12.345680] Call Trace:
[   12.345681]  dump_stack_lvl+0x45/0x5a
[   12.345682]  print_report+0x176/0x4f0
[   12.345683]  kasan_report+0xda/0x110
[   12.345684]  tcp_v4_do_rcv+0x123/0x456
"""

SAME_TRACE_DIFFERENT_ADDR = """\
[   11.111111] BUG: KASAN: use-after-free in tcp_v4_do_rcv+0x123/0x456
[   11.111112] Read of size 8 at addr ffff888099999999 by task swapper/0/2
[   11.111113] Call Trace:
[   11.111114]  dump_stack_lvl+0x45/0x5a
[   11.111115]  print_report+0x176/0x4f0
[   11.111116]  kasan_report+0xda/0x110
[   11.111117]  tcp_v4_do_rcv+0x123/0x456
"""


def test_normalize_removes_addresses():
    norm = normalize_stack(SAMPLE_TRACE)
    assert "ffff888012345678" not in norm
    assert "ADDR" in norm


def test_same_stack_same_signature():
    sig1 = stack_signature(SAMPLE_TRACE)
    sig2 = stack_signature(SAME_TRACE_DIFFERENT_ADDR)
    assert sig1 == sig2


def test_different_stacks_different_signatures():
    other_trace = "BUG: unable to handle kernel NULL pointer dereference\next4_fill_super+0x100/0x200"
    sig1 = stack_signature(SAMPLE_TRACE)
    sig2 = stack_signature(other_trace)
    assert sig1 != sig2


def test_signature_is_hex_string():
    sig = stack_signature(SAMPLE_TRACE)
    assert len(sig) == 32
    int(sig, 16)  # must be valid hex
