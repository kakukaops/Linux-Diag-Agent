"""Unit tests for the hybrid LKML recall route (ADR-025 T-130).

Covers the pure merge/normalize/keyword logic and the L3 lore→Evidence
conversion (with lore_search mocked). The local BM25 path needs a DB and is
integration-tested separately.
"""

from unittest.mock import patch

from retrieval.schema import Evidence, RetrievalQuery, RouteTag
from retrieval.recall import lkml as lkml_recall
from retrieval.recall.lore_search import LoreSearchHit


def _ev(mid: str, score: float, src: str = "l2_cache") -> Evidence:
    return Evidence(route=RouteTag.lkml, score=score, title=f"t-{mid}",
                    body=f"body-{mid}", message_id=mid, metadata={"source": src})


def _query(keywords):
    return RetrievalQuery(raw_question="oom in cgroup", keywords=keywords)


# ── _normalize ──────────────────────────────────────────────────────────────

def test_normalize_to_unit_range():
    evs = [_ev("a", 10.0), _ev("b", 5.0), _ev("c", 0.0)]
    lkml_recall._normalize(evs)
    assert [e.score for e in evs] == [1.0, 0.5, 0.0]


def test_normalize_empty_is_safe():
    lkml_recall._normalize([])  # must not raise


# ── _merge ──────────────────────────────────────────────────────────────────

def test_merge_dedups_and_keeps_local_body():
    local = [_ev("m1", 10.0), _ev("m2", 5.0)]
    remote = [_ev("m2", 1.0, "lore_search"), _ev("m3", 0.5, "lore_search")]
    merged = lkml_recall._merge(local, remote, limit=10)

    by_id = {e.message_id: e for e in merged}
    assert set(by_id) == {"m1", "m2", "m3"}
    # m2 in both: local normalized 5/10=0.5, remote 1.0/1.0=1.0 → lifted to 1.0
    assert by_id["m2"].score == 1.0
    # but body stays the local one (full cached body)
    assert by_id["m2"].metadata["source"] == "l2_cache"
    # results are score-sorted descending
    assert [e.score for e in merged] == sorted((e.score for e in merged), reverse=True)


def test_merge_respects_limit():
    local = [_ev(f"m{i}", float(10 - i)) for i in range(8)]
    merged = lkml_recall._merge(local, [], limit=3)
    assert len(merged) == 3


# ── keyword helpers ─────────────────────────────────────────────────────────

def test_search_keywords_space_joined_and_filtered():
    kws = lkml_recall._search_keywords(_query(["oom", "x", "cgroup", "vmscan"]))
    assert kws == "oom cgroup vmscan"  # 'x' (len<2) dropped


def test_keywords_str_or_joined():
    assert lkml_recall._keywords_str(["oom", "cgroup"]) == "oom OR cgroup"


# ── L3 lore → Evidence conversion ───────────────────────────────────────────

def test_lore_recall_converts_hits_with_rank_scores():
    hits = [
        LoreSearchHit("m1@h", "Title 1", "https://lore.kernel.org/all/m1@h",
                      "2026-04-13T00:00:00Z", "snippet 1"),
        LoreSearchHit("m2@h", "Title 2", "https://lore.kernel.org/all/m2@h",
                      None, "snippet 2"),
    ]
    with patch("retrieval.recall.lore_search.search", return_value=hits):
        out = lkml_recall._lore_recall(_query(["oom"]))

    assert [e.message_id for e in out] == ["m1@h", "m2@h"]
    assert out[0].score > out[1].score          # rank-descending
    assert out[0].body == "snippet 1"
    assert out[0].metadata["source"] == "lore_search"
    assert out[0].metadata["permalink"] == "https://lore.kernel.org/all/m1@h"


# ── recall() offline mode skips L3 ──────────────────────────────────────────

def test_recall_offline_mode_skips_lore_search():
    with patch.object(lkml_recall, "_local_bm25", return_value=[_ev("m1", 1.0)]), \
         patch.object(lkml_recall, "_knowledge_mode", return_value="offline"), \
         patch("retrieval.recall.lore_search.search") as mock_search:
        out = lkml_recall.recall(_query(["oom"]))

    mock_search.assert_not_called()             # offline → no L3
    assert [e.message_id for e in out] == ["m1"]
