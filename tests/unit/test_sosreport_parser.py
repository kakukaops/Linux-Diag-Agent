"""Unit tests for sosreport parser (WBS 4.6)."""

import os
import tarfile
import tempfile
from pathlib import Path

import pytest
from mcp_servers.sosreport.parser import parse_sosreport, SystemSummary, _enrich


def _make_sos_dir(tmp_path: Path, uname: str, hostname: str = "testhost") -> Path:
    sos = tmp_path / "sosreport-test"
    sos.mkdir()
    (sos / "uname").write_text(uname)
    (sos / "hostname").write_text(hostname)
    (sos / "proc").mkdir()
    (sos / "proc" / "uptime").write_text("12345.67 9876.54")
    dmesg = sos / "sos_commands" / "kernel"
    dmesg.mkdir(parents=True)
    (dmesg / "dmesg").write_text("[  1.0] Linux started\n" * 5)
    return sos


def test_parse_dir_olk66(tmp_path):
    sos = _make_sos_dir(
        tmp_path,
        "Linux hostname 6.6.0-55.0.0.olk6.6.x86_64 #1 SMP x86_64 GNU/Linux",
    )
    s = parse_sosreport(str(sos))
    assert s.olk_version_tag == "OLK-6.6"
    assert "6.6.0" in s.kernel_version
    assert s.hostname == "testhost"


def test_parse_dir_olk510(tmp_path):
    sos = _make_sos_dir(
        tmp_path,
        "Linux host 5.10.0-136.12.0.86.olk5.1.x86_64 #1 SMP x86_64 GNU/Linux",
    )
    s = parse_sosreport(str(sos))
    assert s.olk_version_tag == "OLK-5.10"


def test_parse_dir_mainline(tmp_path):
    sos = _make_sos_dir(
        tmp_path,
        "Linux host 6.9.0-rc3 #1 SMP x86_64 GNU/Linux",
    )
    s = parse_sosreport(str(sos))
    assert s.olk_version_tag == "mainline"


def test_dmesg_tail_trimmed(tmp_path):
    sos = _make_sos_dir(
        tmp_path,
        "Linux host 6.6.0.olk6.6 #1 SMP x86_64 GNU/Linux",
    )
    long_dmesg = "\n".join(f"[{i}] line" for i in range(500))
    (sos / "sos_commands" / "kernel" / "dmesg").write_text(long_dmesg)
    s = parse_sosreport(str(sos))
    # dmesg_tail should be at most 200 lines
    assert len(s.dmesg_tail.splitlines()) <= 200


def test_unsupported_path_raises():
    with pytest.raises(ValueError):
        parse_sosreport("/tmp/nonexistent.sos")


def test_to_dict_keys():
    s = SystemSummary(kernel_version="5.10.0", hostname="h", olk_version_tag="OLK-5.10")
    d = s.to_dict()
    for key in ("kernel_version", "hostname", "uptime", "uname", "dmesg_tail", "olk_version_tag"):
        assert key in d
