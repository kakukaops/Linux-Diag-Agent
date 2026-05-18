"""Unit tests for OLK inclusion header parser (ADR-018 C1)."""

import pytest
from ingest.kernel_commit.olk_inclusion import parse_olk_inclusion, OlkInclusionInfo


STABLE_BODY = """\
stable inclusion
from stable-v6.6.124
commit 7c54d3f5ebbc5982daaa004260242dc07ac943ea
category: bugfix
bugzilla: https://atomgit.com/src-openeuler/kernel/issues/13892
CVE: CVE-2026-23261
Reference: https://git.kernel.org/stable/linux.git/commit/?id=7c54d3f5...

--------------------------------

[ Upstream commit d1877cc7270302081a315a81a0ee8331f19f95c8 ]

Fix a null pointer dereference in the TCP stack.
"""

MAINLINE_BODY = """\
mainline inclusion
from mainline-v6.6
commit abc123def4560000000000000000000000000000
category: feature

Some feature description.
"""

HULK_BODY = """\
hulk inclusion
from hulk-v6.6
commit deadbeef00000000000000000000000000000000
category: performance

openEuler-specific performance patch.
"""

MR_MERGE_BODY = """\
Merge request from feature branch.

See merge request openeuler/kernel!12345
"""

NO_HEADER_BODY = """\
Just a regular commit with no inclusion header.

Fixes: abc1234
"""


def test_stable_inclusion_parsed():
    info = parse_olk_inclusion("Fix TCP null deref", STABLE_BODY)
    assert info.kind == "backport"
    assert info.tag == "stable"
    assert info.head_commit == "7c54d3f5ebbc5982daaa004260242dc07ac943ea"
    assert info.upstream_commit == "d1877cc7270302081a315a81a0ee8331f19f95c8"
    assert info.best_upstream_sha == "d1877cc7270302081a315a81a0ee8331f19f95c8"
    assert info.is_backport is True


def test_mainline_inclusion_parsed():
    info = parse_olk_inclusion("Add feature", MAINLINE_BODY)
    assert info.kind == "backport"
    assert info.tag == "mainline"
    assert info.head_commit == "abc123def4560000000000000000000000000000"
    assert info.upstream_commit is None
    assert info.best_upstream_sha == "abc123def4560000000000000000000000000000"


def test_hulk_native():
    info = parse_olk_inclusion("Performance patch", HULK_BODY)
    assert info.kind == "native"
    assert info.tag == "hulk"
    assert info.is_backport is False
    assert info.best_upstream_sha is None


def test_mr_merge_commit():
    info = parse_olk_inclusion("Merge feature branch", MR_MERGE_BODY)
    assert info.kind == "merge"


def test_no_inclusion_header():
    info = parse_olk_inclusion("Regular fix", NO_HEADER_BODY)
    assert info.kind == "no_header"


def test_stable_without_upstream_commit():
    body = """\
stable inclusion
from stable-v6.6.100
commit aabbccdd00000000000000000000000000000000
category: bugfix

--------------------------------

No upstream commit line here.
"""
    info = parse_olk_inclusion("Fix something", body)
    assert info.kind == "backport"
    assert info.tag == "stable"
    assert info.upstream_commit is None
    # Falls back to head_commit (stable SHA)
    assert info.best_upstream_sha == "aabbccdd00000000000000000000000000000000"
