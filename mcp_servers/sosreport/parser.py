"""sosreport parser (WBS 4.6) — extracts SystemSummary from an sos archive.

Handles both .tar.xz and directory-extracted sosreports.
Key outputs:
  - kernel_version: str  (e.g. "5.10.0-136.12.0.86.olk5.1.x86_64")
  - hostname: str
  - uptime: str
  - dmesg_tail: str      (last 200 lines of dmesg from sos)
  - uname: str
  - olk_version_tag: str (OLK-6.6 / OLK-5.10 / mainline / unknown)
"""

from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SystemSummary:
    kernel_version: str = ""
    hostname: str = ""
    uptime: str = ""
    uname: str = ""
    dmesg_tail: str = ""
    olk_version_tag: str = "unknown"
    raw_metadata: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw_metadata is None:
            self.raw_metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kernel_version": self.kernel_version,
            "hostname": self.hostname,
            "uptime": self.uptime,
            "uname": self.uname,
            "dmesg_tail": self.dmesg_tail,
            "olk_version_tag": self.olk_version_tag,
        }


# OLK version detection patterns
_OLK66_RE = re.compile(r"olk6[\._]?6|OLK-6\.6", re.IGNORECASE)
# OLK-5.10 kernels show as "5.10.x-...olk5..." in uname
_OLK510_RE = re.compile(r"olk5[\._]?(?:10|1)|OLK-5\.10", re.IGNORECASE)
_KVER_RE = re.compile(r"^(\d+)\.(\d+)\.")


def parse_sosreport(path: str | Path) -> SystemSummary:
    """Parse a sosreport tarball or extracted directory.

    Returns a SystemSummary with available fields populated.
    """
    p = Path(path)
    if p.is_dir():
        return _parse_dir(p)
    if p.suffix in (".xz", ".gz", ".bz2") or p.name.endswith(".tar.xz"):
        return _parse_tar(p)
    raise ValueError(f"Unsupported sosreport path: {path}")


def _parse_tar(path: Path) -> SystemSummary:
    summary = SystemSummary()
    try:
        with tarfile.open(str(path), "r:*") as tar:
            members = {m.name for m in tar.getmembers()}
            summary.uname = _read_member(tar, members, "uname", "sos_commands/kernel/uname_-a")
            summary.hostname = _read_member(tar, members, "hostname")
            summary.uptime = _read_member(tar, members, "uptime", "proc/uptime")
            dmesg = _read_member(tar, members, "sos_commands/kernel/dmesg", "var/log/dmesg")
            if dmesg:
                summary.dmesg_tail = "\n".join(dmesg.splitlines()[-200:])
    except Exception:
        pass
    _enrich(summary)
    return summary


def _parse_dir(root: Path) -> SystemSummary:
    summary = SystemSummary()
    # Walk common sos directory layout
    for candidate in [
        "uname",
        "sos_commands/kernel/uname_-a",
        "sos_commands/uname/uname_-a",
    ]:
        f = root / candidate
        if f.exists():
            summary.uname = f.read_text(errors="replace").strip()
            break
    for candidate in ["hostname", "etc/hostname"]:
        f = root / candidate
        if f.exists():
            summary.hostname = f.read_text(errors="replace").strip()
            break
    for candidate in ["proc/uptime", "uptime"]:
        f = root / candidate
        if f.exists():
            summary.uptime = f.read_text(errors="replace").strip()
            break
    for candidate in ["sos_commands/kernel/dmesg", "var/log/dmesg", "dmesg"]:
        f = root / candidate
        if f.exists():
            dmesg = f.read_text(errors="replace")
            summary.dmesg_tail = "\n".join(dmesg.splitlines()[-200:])
            break
    _enrich(summary)
    return summary


def _enrich(summary: SystemSummary) -> None:
    """Extract kernel_version and OLK tag from uname string."""
    if summary.uname:
        parts = summary.uname.split()
        if len(parts) >= 3:
            summary.kernel_version = parts[2]
        kv = summary.kernel_version
        uname_full = summary.uname
        if _OLK66_RE.search(uname_full):
            summary.olk_version_tag = "OLK-6.6"
        elif _OLK510_RE.search(uname_full):
            summary.olk_version_tag = "OLK-5.10"
        elif "olk" in uname_full.lower():
            # Unknown OLK branch — infer from base version
            m = _KVER_RE.match(kv)
            if m and m.group(1) == "5" and m.group(2) == "10":
                summary.olk_version_tag = "OLK-5.10"
            elif m and m.group(1) == "6" and m.group(2) == "6":
                summary.olk_version_tag = "OLK-6.6"
            else:
                summary.olk_version_tag = "unknown"
        elif kv:
            summary.olk_version_tag = "mainline"


def _read_member(tar: tarfile.TarFile, members: set[str], *candidates: str) -> str:
    for cand in candidates:
        # sosreport tarballs typically have a top-level prefix dir
        matching = [m for m in members if m.endswith("/" + cand) or m == cand]
        if matching:
            try:
                f = tar.extractfile(matching[0])
                if f:
                    return f.read().decode(errors="replace").strip()
            except Exception:
                pass
    return ""
