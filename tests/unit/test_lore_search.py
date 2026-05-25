"""Unit tests for the L3 lore live-search client (ADR-025)."""

from retrieval.recall.lore_search import _parse_search_atom, search


_SAMPLE_ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>[PATCH] mm: fix oom</title>
    <updated>2026-04-13T21:33:35Z</updated>
    <link href="https://lore.kernel.org/all/20260413.abc@host/"/>
    <summary>snippet text here</summary>
  </entry>
  <entry>
    <title>Re: [PATCH] mm: fix oom</title>
    <updated>2026-04-14T10:00:00Z</updated>
    <link href="https://lore.kernel.org/all/20260414.def@host/"/>
  </entry>
</feed>"""


def test_parse_search_atom_extracts_hits():
    hits = _parse_search_atom(_SAMPLE_ATOM)
    assert len(hits) == 2
    assert hits[0].message_id == "20260413.abc@host"
    assert hits[0].title == "[PATCH] mm: fix oom"
    assert hits[0].permalink == "https://lore.kernel.org/all/20260413.abc@host"
    assert hits[0].updated == "2026-04-13T21:33:35Z"
    assert hits[0].snippet == "snippet text here"


def test_parse_search_atom_missing_summary():
    hits = _parse_search_atom(_SAMPLE_ATOM)
    assert hits[1].message_id == "20260414.def@host"
    assert hits[1].snippet == ""  # no <summary>/<content>


def test_parse_search_atom_skips_non_msgid_links():
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>noise</title><link href="https://lore.kernel.org/all/"/></entry>
    </feed>"""
    assert _parse_search_atom(atom) == []  # last path segment has no '@'


def test_parse_search_atom_empty_and_malformed():
    assert _parse_search_atom(b"") == []
    assert _parse_search_atom(b"<not valid xml") == []


def test_search_empty_keywords_returns_empty():
    # no network call should happen for blank input
    assert search("") == []
    assert search("   ") == []
