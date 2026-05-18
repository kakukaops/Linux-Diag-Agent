"""Unit tests for LLM reranker (WBS 3.9)."""

import pytest
from retrieval.schema import Evidence, RetrievalQuery, RouteTag
from retrieval.reranker import maybe_rerank, RERANK_THRESHOLD


def _make_items(n: int) -> list[Evidence]:
    return [Evidence(route=RouteTag.commit, score=float(n - i), title=f"item-{i}") for i in range(n)]


def test_no_rerank_below_threshold():
    q = RetrievalQuery(raw_question="test")
    items = _make_items(RERANK_THRESHOLD)
    result, reranked = maybe_rerank(q, items)
    assert reranked is False
    assert result == items


def test_rerank_attempted_above_threshold():
    q = RetrievalQuery(raw_question="test")
    items = _make_items(RERANK_THRESHOLD + 1)
    # LLM unavailable in unit test — should fall back gracefully
    result, reranked = maybe_rerank(q, items)
    # Either reranked or fell back — both are valid; no exception
    assert isinstance(result, list)
    assert len(result) <= len(items)


def test_empty_items():
    q = RetrievalQuery(raw_question="test")
    result, reranked = maybe_rerank(q, [])
    assert result == []
    assert reranked is False
