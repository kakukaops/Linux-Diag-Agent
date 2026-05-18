"""Subsystem inference from changed file paths.

Strategy: take the longest common directory prefix of all changed files.
e.g. ['mm/slab.c', 'mm/slub.c'] → 'mm'
     ['drivers/net/ethernet/intel/e1000e/netdev.c'] → 'drivers/net/ethernet/intel/e1000e'
     ['Makefile', 'mm/foo.c'] → '' (top-level, no clear subsystem)
"""

from __future__ import annotations

import os


def infer_subsystem(changed_files: list[str]) -> str | None:
    """Return the most-specific common subsystem directory, or None."""
    if not changed_files:
        return None

    # Filter out top-level files (no directory component)
    dirs = [os.path.dirname(f) for f in changed_files if "/" in f]
    if not dirs:
        return None

    # Find longest common prefix path
    common = os.path.commonprefix(dirs)
    # Trim to the last complete directory segment
    common = common.rstrip("/")
    if not common:
        return None

    # Normalize: if common is a very deep path, return top 3 levels max
    parts = common.split("/")
    return "/".join(parts[:3])
