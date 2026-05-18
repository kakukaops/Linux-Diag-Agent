"""Unit tests for retrieval schema and query parser (WBS 3.6/3.10)."""

import pytest
from retrieval.schema import RetrievalQuery, Evidence, RouteTag, RetrievalResult
from retrieval.query_parser import _regex_parse


def test_default_routes_all_seven():
    q = RetrievalQuery(raw_question="test")
    assert set(q.routes) == set(RouteTag)


def test_evidence_route_tag():
    ev = Evidence(route=RouteTag.commit, score=0.9, title="fix: mm OOM")
    assert ev.route == RouteTag.commit
    assert ev.commit_hash is None


def test_retrieval_result_empty():
    q = RetrievalQuery(raw_question="foo")
    r = RetrievalResult(query=q)
    assert r.items == []
    assert r.reranked is False


def test_regex_parse_cve():
    q = _regex_parse("How does CVE-2024-12345 affect OLK-6.6?")
    assert "CVE-2024-12345" in q.cve_ids
    assert q.kernel_version == "OLK-6.6"


def test_regex_parse_version_v5():
    q = _regex_parse("v5.10 tcp null pointer dereference")
    assert q.kernel_version == "OLK-5.10"


def test_regex_parse_keywords():
    q = _regex_parse("kasan use-after-free in tcp_v4_do_rcv")
    assert any("kasan" in kw for kw in q.keywords)
    assert any("tcp" in kw for kw in q.keywords)


def test_regex_parse_commit_hash():
    q = _regex_parse("commit abc1234def5678 introduced the bug")
    assert any("abc1234" in h for h in q.commit_hashes)
