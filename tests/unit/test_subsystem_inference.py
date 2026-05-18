"""Unit tests for subsystem inference."""

import pytest
from ingest.kernel_commit.subsystem_inference import infer_subsystem


def test_mm_subsystem():
    assert infer_subsystem(["mm/slab.c", "mm/slub.c"]) == "mm"


def test_drivers_net():
    files = ["drivers/net/ethernet/intel/e1000e/netdev.c"]
    result = infer_subsystem(files)
    assert result is not None
    assert result.startswith("drivers/net")


def test_top_level_files():
    result = infer_subsystem(["Makefile", "README"])
    assert result is None


def test_mixed_files():
    result = infer_subsystem(["fs/ext4/inode.c", "fs/ext4/super.c"])
    assert result is not None
    assert "fs" in result


def test_empty():
    assert infer_subsystem([]) is None


def test_no_path_separator():
    result = infer_subsystem(["Kconfig", "MAINTAINERS"])
    assert result is None
