"""SOP registry — load and cache YAML SOP definitions (WBS 7.3).

SOP files live in agent/sop/definitions/<name>.yaml.
Falls back to generic_diagnosis.yaml if the named SOP is not found.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

_SOP_DIR = Path(__file__).parent / "definitions"


@functools.lru_cache(maxsize=32)
def get_sop(name: str) -> dict[str, Any]:
    """Load SOP by name. Raises KeyError if neither the SOP nor generic exist."""
    path = _SOP_DIR / f"{name}.yaml"
    if path.exists():
        return _load(path)
    fallback = _SOP_DIR / "generic.yaml"
    if fallback.exists():
        return _load(fallback)
    raise KeyError(f"SOP '{name}' not found and no generic fallback")


def list_sops() -> list[str]:
    """Return names of all available SOPs."""
    return [p.stem for p in _SOP_DIR.glob("*.yaml")]


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
