"""Unit tests for commit trailer parser."""

import pytest
from ingest.kernel_commit.trailer_parser import parse_trailers


def test_fixes_extraction():
    body = "Fixes: abc1234def567 (some title)\nSigned-off-by: foo@bar.com"
    t = parse_trailers(body)
    assert "abc1234def567" in t.fixes_refs


def test_multiple_fixes():
    body = "Fixes: aabbccdd1234\nFixes: eeff00112233 (another fix)"
    t = parse_trailers(body)
    assert len(t.fixes_refs) == 2


def test_reported_by():
    body = "Reported-by: Alice <alice@example.com>\nReported-by: syzbot+abcd@syzkaller.appspotmail.com"
    t = parse_trailers(body)
    assert len(t.reported_by) == 2


def test_closes_url():
    body = "Closes: https://bugzilla.kernel.org/show_bug.cgi?id=12345"
    t = parse_trailers(body)
    assert len(t.closes_refs) == 1
    assert "bugzilla.kernel.org" in t.closes_refs[0]


def test_no_trailers():
    body = "Just a plain commit message without any trailers."
    t = parse_trailers(body)
    assert t.fixes_refs == []
    assert t.reported_by == []
    assert t.closes_refs == []


def test_link_extraction():
    body = "Link: https://lore.kernel.org/r/20260514.143@kernel.org"
    t = parse_trailers(body)
    assert len(t.link_urls) == 1
