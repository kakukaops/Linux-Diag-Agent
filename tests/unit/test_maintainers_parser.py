"""Tests for scripts/ingest_maintainers.py — MAINTAINERS file parser."""

import importlib.util
import sys
from pathlib import Path


# Load the parser module from scripts/ (not a normal Python package).
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ingest_maintainers.py"
_spec = importlib.util.spec_from_file_location("ingest_maintainers", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ingest_maintainers"] = _mod
_spec.loader.exec_module(_mod)
parse = _mod.parse


_SAMPLE = """\
Header text before list.

Maintainers List
----------------

3C59X NETWORK DRIVER
M:\tSteffen Klassert <klassert@kernel.org>
L:\tnetdev@vger.kernel.org
S:\tOdd Fixes
F:\tDocumentation/networking/device_drivers/ethernet/3com/vortex.rst
F:\tdrivers/net/ethernet/3com/3c59x.c

NETWORKING [TCP]
M:\tEric Dumazet <edumazet@google.com>
M:\tNeal Cardwell <ncardwell@google.com>
R:\tDavid Ahern <dsahern@kernel.org>
L:\tnetdev@vger.kernel.org
S:\tMaintained
T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/netdev/net.git
F:\tnet/ipv4/tcp*.c
F:\tnet/ipv4/tcp_*.c
X:\tnet/ipv4/tcp_debug.c
N:\ttcp_.*\\.c
"""


def test_parse_basic_section(tmp_path):
    p = tmp_path / "MAINTAINERS"
    p.write_text(_SAMPLE)
    sections = parse(p)

    assert len(sections) == 2
    s = sections[0]
    assert s.name == "3C59X NETWORK DRIVER"
    assert s.status == "Odd Fixes"
    assert s.mailing_list == "netdev@vger.kernel.org"
    assert len(s.persons) == 1
    role, name, email = s.persons[0]
    assert role == "M"
    assert name == "Steffen Klassert"
    assert email == "klassert@kernel.org"
    assert any(k == "F" for k, _ in s.files)


def test_parse_multi_maintainer_and_reviewer(tmp_path):
    p = tmp_path / "MAINTAINERS"
    p.write_text(_SAMPLE)
    sections = parse(p)

    s = sections[1]
    assert s.name == "NETWORKING [TCP]"
    roles = [r for r, _, _ in s.persons]
    assert roles.count("M") == 2
    assert roles.count("R") == 1
    emails = {e for _, _, e in s.persons}
    assert "edumazet@google.com" in emails
    assert "ncardwell@google.com" in emails
    assert "dsahern@kernel.org" in emails


def test_parse_file_patterns_split_by_kind(tmp_path):
    p = tmp_path / "MAINTAINERS"
    p.write_text(_SAMPLE)
    sections = parse(p)

    s = sections[1]
    kinds = [k for k, _ in s.files]
    assert kinds.count("F") == 2
    assert kinds.count("X") == 1
    assert kinds.count("N") == 1
    patterns_f = {p for k, p in s.files if k == "F"}
    assert "net/ipv4/tcp*.c" in patterns_f


def test_parse_git_tree_captures_first(tmp_path):
    p = tmp_path / "MAINTAINERS"
    p.write_text(_SAMPLE)
    sections = parse(p)
    s = sections[1]
    assert s.git_tree is not None
    assert "kernel.org" in s.git_tree


def test_parse_header_before_marker_is_skipped(tmp_path):
    """Lines before 'Maintainers List' / '----' must not produce a section."""
    p = tmp_path / "MAINTAINERS"
    p.write_text("Some header.\n\nMore header.\n\n----\nFOO\nM:\tx <a@b>\n")
    sections = parse(p)
    names = [s.name for s in sections]
    assert "Some header." not in names
    assert "More header." not in names
    assert "FOO" in names
