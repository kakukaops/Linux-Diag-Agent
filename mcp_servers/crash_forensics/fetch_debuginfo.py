"""fetch_debuginfo: locate vmlinux for vmcore analysis (T-014).

Checks standard locations in order:
  1. OLK source tree build output (primary for dev)
  2. /boot/vmlinux-<version> (typical RPM install)
  3. debuginfo RPM cache under /var/cache/debuginfo/
  4. A path extracted from a sosreport (if sosreport_path given)

This is intentionally simple: we don't fetch from remote RPM repos or
package managers here. That's a v2.1 enhancement if needed.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import Any


# OLK kernel source build directories (CLAUDE.md ADR-018)
_OLK_BUILD_DIRS: dict[str, Path] = {
    "OLK-6.6": Path("/data1/lingqu/codes/OLK-6.6/kernel"),
    "OLK-5.10": Path("/data1/lingqu/codes/OLK-5.10/kernel"),
}

_DEBUGINFO_CACHE = Path("/var/cache/diag-agent/debuginfo")


def fetch_debuginfo(
    kernel_version: str | None = None,
    sosreport_path: str | None = None,
) -> dict[str, Any]:
    """Locate vmlinux for the given kernel version.

    Args:
        kernel_version: e.g. "OLK-6.6", "5.10.0-153.oe2203sp3.x86_64"
        sosreport_path: path to a sosreport directory/archive; will search
                        for vmlinux inside if provided.

    Returns:
        dict with 'vmlinux_path' (str) and 'source' (how it was found),
        or 'error' key if not found.
    """
    # 1. OLK source tree build output
    if kernel_version:
        for tag, build_dir in _OLK_BUILD_DIRS.items():
            if kernel_version in tag or tag in kernel_version:
                vmlinux = build_dir / "vmlinux"
                if vmlinux.exists():
                    return {
                        "vmlinux_path": str(vmlinux),
                        "source": f"olk_build_dir:{build_dir}",
                        "kernel_version": kernel_version,
                    }

    # 2. Best-match among all OLK build dirs (most recently built)
    best: tuple[float, Path] | None = None
    for build_dir in _OLK_BUILD_DIRS.values():
        vmlinux = build_dir / "vmlinux"
        if vmlinux.exists():
            mtime = vmlinux.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, vmlinux)
    if best:
        return {
            "vmlinux_path": str(best[1]),
            "source": "olk_build_dir_fallback",
            "kernel_version": kernel_version,
        }

    # 3. /boot/vmlinux-<uname>
    boot = Path("/boot")
    if boot.exists():
        if kernel_version:
            # Try exact match first
            exact = boot / f"vmlinux-{kernel_version}"
            if exact.exists():
                return {"vmlinux_path": str(exact), "source": "boot", "kernel_version": kernel_version}
        # Newest vmlinux in /boot
        candidates = sorted(boot.glob("vmlinux-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return {
                "vmlinux_path": str(candidates[0]),
                "source": "boot_latest",
                "kernel_version": kernel_version,
            }

    # 4. Debuginfo RPM cache
    if _DEBUGINFO_CACHE.exists() and kernel_version:
        for path in _DEBUGINFO_CACHE.rglob("vmlinux"):
            if kernel_version in str(path):
                return {"vmlinux_path": str(path), "source": "debuginfo_cache", "kernel_version": kernel_version}

    # 5. Extract from sosreport archive
    if sosreport_path:
        result = _find_vmlinux_in_sosreport(sosreport_path)
        if result:
            return result

    return {
        "error": "vmlinux_not_found",
        "detail": (
            f"Could not locate vmlinux for kernel_version={kernel_version!r}. "
            "Checked: OLK source build dirs, /boot, debuginfo cache. "
            "Options: 1) Build OLK kernel with debug info in the standard source path. "
            "2) Install kernel-debuginfo RPM. "
            "3) Provide vmlinux_path explicitly."
        ),
        "searched": [str(d) for d in _OLK_BUILD_DIRS.values()] + ["/boot"],
    }


def _find_vmlinux_in_sosreport(sosreport_path: str) -> dict[str, Any] | None:
    """Try to find a vmlinux inside a sosreport archive or directory."""
    p = Path(sosreport_path)
    if not p.exists():
        return None

    if p.is_dir():
        # Uncompressed sosreport directory
        candidates = list(p.rglob("vmlinux"))
        if candidates:
            return {
                "vmlinux_path": str(candidates[0]),
                "source": "sosreport_dir",
            }
        return None

    # Compressed archive (.tar.xz / .tar.gz / .tar.bz2)
    if tarfile.is_tarfile(str(p)):
        extract_dir = _DEBUGINFO_CACHE / "sos_extract" / p.stem
        try:
            extract_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(str(p)) as tf:
                # Only extract vmlinux (avoid full extraction of large archives)
                vmlinux_members = [m for m in tf.getmembers()
                                   if m.name.endswith("vmlinux") and m.size < 500 * 1024 * 1024]
                if vmlinux_members:
                    tf.extractall(path=str(extract_dir), members=vmlinux_members)
                    extracted = list(extract_dir.rglob("vmlinux"))
                    if extracted:
                        return {"vmlinux_path": str(extracted[0]), "source": "sosreport_archive"}
        except Exception:
            pass

    return None
