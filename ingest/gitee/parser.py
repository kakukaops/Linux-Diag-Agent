"""Map a gitee issue JSON to the canonical bug-table dict.

Gitee API v5 issue schema (relevant subset):
  number      str   alphanumeric ID, e.g. "IDCSJV"
  title       str
  body        str   markdown
  state       str   "open" | "progressing" | "closed" | "rejected"
  user.login  str   reporter
  assignee.login str
  labels      list[{name, ...}]
  priority    int   0-3 (0=low, 3=high)
  issue_type  str   e.g. "内核缺陷"
  created_at  ISO8601
  updated_at  ISO8601
  finished_at ISO8601 | None
"""

from __future__ import annotations

import re
from typing import Any

# OpenEuler kernel version pattern in issue bodies.
_KVER_RE = re.compile(
    r"kernel[- ]?(\d+\.\d+\.\d+(?:[-.][\w.]+)?)",
    re.IGNORECASE,
)
# Subsystem label is the first label matching "sig/Foo" or "kind/foo".
_SUBSYSTEM_LABEL_RE = re.compile(r"^sig/(\S+)$", re.IGNORECASE)


_PRIORITY_TO_SEVERITY = {0: "low", 1: "medium", 2: "high", 3: "critical"}


def _ts(v: Any) -> Any:
    """Empty-string → None for timestamp columns (PG rejects '')."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


def parse_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert gitee issue JSON to bug-table row dict."""
    labels = raw.get("labels") or []
    label_names = [lab.get("name", "") for lab in labels]
    subsystem = None
    for n in label_names:
        m = _SUBSYSTEM_LABEL_RE.match(n)
        if m:
            subsystem = m.group(1).lower()
            break

    body = raw.get("body") or ""
    kernel_versions = sorted(set(_KVER_RE.findall(body)))

    priority = raw.get("priority")
    if isinstance(priority, int):
        severity = _PRIORITY_TO_SEVERITY.get(priority)
    else:
        severity = None

    return {
        "source": "gitee",
        "external_id": str(raw["number"]),
        "title": (raw.get("title") or "")[:1024],
        "status": raw.get("state"),
        "component": raw.get("issue_type"),
        "subsystem": subsystem,
        "severity": severity,
        "reporter": (raw.get("user") or {}).get("login"),
        "assignee": (raw.get("assignee") or {}).get("login"),
        "created_at": _ts(raw.get("created_at")),
        "updated_at": _ts(raw.get("updated_at")),
        "closed_at": _ts(raw.get("finished_at")),
        "kernel_versions": kernel_versions,
        "description": body,
    }
