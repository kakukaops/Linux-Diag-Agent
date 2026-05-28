"""Regression tests for ingest/syzbot/scraper.py + ingester.py.

Pre-v2.3 the scraper only walked /upstream (open ≈ 1.3K bugs), missing
/upstream/fixed (≈ 5.8K, each with a Fix: commit hash) and
/upstream/invalid (≈ 3.4K). Combined with a `new_ids[:200]` hard cap in
the ingester, only 999 rows landed out of ~10.5K available.

These tests pin the v2.3 behavior so the regression cannot return
silently.
"""

from unittest.mock import MagicMock, patch
from pathlib import Path

from ingest.syzbot.scraper import SyzbotScraper


# ── scraper.list_crashes walks ALL 3 dashboards ───────────────────────────────

_SAMPLE_HTML = """
<html><body>
<table class="list_table">
<tr><td><a href="/bug?extid=aaaa">Title A</a></td><td>moderation</td></tr>
<tr><td><a href="/bug?extid=bbbb">Title B</a></td><td>biz</td></tr>
</table>
</body></html>
"""

_SAMPLE_HTML_FIXED = """
<html><body>
<table class="list_table">
<tr><td><a href="/bug?extid=cccc">Fixed Title C</a></td><td>fixed</td></tr>
</table>
</body></html>
"""

_SAMPLE_HTML_INVALID = """
<html><body>
<table class="list_table">
<tr><td><a href="/bug?extid=dddd">Invalid D</a></td><td>invalid</td></tr>
</table>
</body></html>
"""


def test_scraper_walks_all_three_dashboards(tmp_path):
    scraper = SyzbotScraper(tmp_path)
    # Patch the _get method to return different HTML per URL
    def fake_get(url):
        if url.endswith("/upstream"):           return _SAMPLE_HTML
        if url.endswith("/upstream/fixed"):     return _SAMPLE_HTML_FIXED
        if url.endswith("/upstream/invalid"):   return _SAMPLE_HTML_INVALID
        return ""
    scraper._get = MagicMock(side_effect=fake_get)

    crashes = scraper.list_crashes()

    # Three URLs visited
    visited = {c.args[0] for c in scraper._get.call_args_list}
    assert any(u.endswith("/upstream") for u in visited)
    assert any(u.endswith("/upstream/fixed") for u in visited)
    assert any(u.endswith("/upstream/invalid") for u in visited)

    # All 4 unique crashes appear
    ids = {c["id"] for c in crashes}
    assert ids == {"aaaa", "bbbb", "cccc", "dddd"}

    # Each row carries the dashboard label
    by_id = {c["id"]: c for c in crashes}
    assert by_id["cccc"]["dashboard"] == "fixed"
    assert by_id["dddd"]["dashboard"] == "invalid"
    assert by_id["aaaa"]["dashboard"] == "open"


def test_scraper_prioritises_fixed_first(tmp_path):
    """Fixed crashes carry the Fix: commit — most valuable, should be
    processed first by the ingester."""
    scraper = SyzbotScraper(tmp_path)
    def fake_get(url):
        if "/fixed" in url:    return _SAMPLE_HTML_FIXED
        if "/invalid" in url:  return _SAMPLE_HTML_INVALID
        return _SAMPLE_HTML
    scraper._get = MagicMock(side_effect=fake_get)

    crashes = scraper.list_crashes()
    # First item must be from 'fixed' dashboard
    assert crashes[0]["dashboard"] == "fixed"


def test_scraper_dedups_same_id_across_dashboards(tmp_path):
    """If a crash appears on multiple dashboards (shouldn't happen but
    network races could produce it), keep only one row."""
    scraper = SyzbotScraper(tmp_path)
    dup_html = _SAMPLE_HTML.replace("aaaa", "aaaa")  # same id
    scraper._get = MagicMock(return_value=dup_html)

    crashes = scraper.list_crashes()
    ids = [c["id"] for c in crashes]
    assert len(ids) == len(set(ids))


# ── ingester drops the 200-row cap; processes 'fixed' even if already seen ────

def test_ingester_revisits_fixed_dashboard_entries():
    """A crash transitioning open → fixed must be re-fetched to capture
    the Fix: commit that just appeared, even though we've seen its id."""
    from ingest.syzbot.ingester import SyzbotIngester
    from ingest.base import RunReport, Quarantine

    # Build a fake ingester that doesn't hit real DB/network
    ing = SyzbotIngester.__new__(SyzbotIngester)
    ing._data_dir = Path("/tmp")
    ing._engine = MagicMock()
    ing.source_name = "syzbot"

    seen = {"aaaa"}     # we already saw aaaa in a previous run as 'open'
    checkpoint = {"seen_crash_ids": list(seen)}

    crashes = [
        {"id": "aaaa", "title": "...", "status": "fixed", "dashboard": "fixed"},
        {"id": "bbbb", "title": "...", "status": "open",  "dashboard": "open"},
    ]

    fake_detail = MagicMock(
        syzbot_id="aaaa", title="t", status="fixed",
        fix_commit="abc123", stack_trace="trace", stack_signature="sig",
    )

    with patch("ingest.syzbot.ingester.SyzbotScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.list_crashes.return_value = crashes
        mock_scraper.fetch_crash_detail.return_value = fake_detail
        mock_scraper.save_crash = MagicMock()

        report = RunReport(source="syzbot")
        quarantine = MagicMock()
        ing._upsert_crash = MagicMock()

        ing.incremental(checkpoint, report, quarantine)

    # Both aaaa (re-fetched because moved to fixed) and bbbb (new) should
    # have been fetched. Pre-v2.3 would skip aaaa as "already seen".
    fetched = [c.args[0] for c in mock_scraper.fetch_crash_detail.call_args_list]
    assert "aaaa" in fetched, "must re-fetch when crash moved to fixed dashboard"
    assert "bbbb" in fetched


def test_ingester_does_not_cap_at_200():
    """Old `new_ids[:200]` was the LIMIT-trap pattern. The v2.3 cap is
    raised to _MAX_PER_RUN=2000 (courtesy ceiling for a single weekly
    run). Crucially: the SOURCE of the cap is now a named class
    constant, not a magic literal in the middle of a loop."""
    from ingest.syzbot.ingester import SyzbotIngester
    assert hasattr(SyzbotIngester, "_MAX_PER_RUN")
    assert SyzbotIngester._MAX_PER_RUN >= 1000, \
        "cap should accommodate full syzbot dashboards (~10K total)"
