"""Bugzilla status / severity normalization."""

from __future__ import annotations

# kernel.org BZ status → normalized status
STATUS_MAP: dict[str, str] = {
    "NEW": "new",
    "ASSIGNED": "in_progress",
    "IN_PROGRESS": "in_progress",
    "REOPENED": "new",
    "RESOLVED": "resolved",
    "VERIFIED": "resolved",
    "CLOSED": "closed",
    "NEEDINFO": "need_info",
}

RESOLUTION_MAP: dict[str, str] = {
    "FIXED": "fixed",
    "INVALID": "invalid",
    "WONTFIX": "wont_fix",
    "DUPLICATE": "duplicate",
    "WORKSFORME": "works_for_me",
    "NOTABUG": "not_a_bug",
    "": "",
}

SEVERITY_MAP: dict[str, str] = {
    "blocker": "critical",
    "critical": "critical",
    "major": "major",
    "normal": "normal",
    "minor": "minor",
    "trivial": "trivial",
    "enhancement": "enhancement",
}


def normalize_status(raw: str) -> str:
    return STATUS_MAP.get(raw.upper(), raw.lower())


def normalize_resolution(raw: str) -> str:
    return RESOLUTION_MAP.get(raw.upper(), raw.lower())


def normalize_severity(raw: str) -> str:
    return SEVERITY_MAP.get(raw.lower(), raw.lower())
