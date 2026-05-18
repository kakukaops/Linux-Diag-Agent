"""Unit tests for SOP registry (WBS 7.3)."""

import pytest
from agent.sop.registry import get_sop, list_sops


def test_generic_sop_loads():
    sop = get_sop("generic")
    assert "steps" in sop
    assert len(sop["steps"]) > 0


def test_oom_sop_loads():
    sop = get_sop("oom")
    assert sop["name"] == "oom"
    assert "steps" in sop


def test_lockup_sop_loads():
    sop = get_sop("lockup")
    assert sop["name"] == "lockup"


def test_panic_sop_loads():
    sop = get_sop("panic")
    assert sop["name"] == "panic"


def test_unknown_falls_back_to_generic():
    sop = get_sop("nonexistent_sop_xyz")
    assert "steps" in sop  # generic fallback


def test_list_sops():
    sops = list_sops()
    assert "generic" in sops
    assert "oom" in sops
    assert "lockup" in sops
    assert "panic" in sops
