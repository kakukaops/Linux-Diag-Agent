"""Atomgit issue JSON → bug row. JSON shape is identical to gitee v5;
re-use the gitee parser and just override the source tag.
"""

from __future__ import annotations

from typing import Any

from ingest.gitee.parser import parse_issue as _parse_gitee


def parse_issue(raw: dict[str, Any]) -> dict[str, Any]:
    row = _parse_gitee(raw)
    row["source"] = "atomgit"
    return row
