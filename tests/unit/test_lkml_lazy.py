"""Unit tests for the L2 lazy read-through cache core (ADR-025).

Covers `fetch_and_persist` — the shared fetch+parse+upsert primitive used by
both the L2 lazy cache and the L1 backfill. The DB-backed read-through paths
(`get_message` / `ensure_cached`) are integration-tested separately.
"""

from unittest.mock import MagicMock

import pytest

from ingest.lkml.lazy import fetch_and_persist, _summary_decision


_RAW_EMAIL = b"""From mboxrd@z Thu Jan  1 00:00:00 1970
Message-ID: <test123@example.com>
From: Tester <t@example.com>
Subject: [PATCH] test fix
Date: Mon, 13 Apr 2026 21:33:35 +0000

Body text of the patch.
"""


def _mock_deps():
    fetcher = MagicMock()
    ingester = MagicMock()
    thread_builder = MagicMock()
    thread_builder.get_or_create_thread.return_value = 42
    return fetcher, ingester, thread_builder


def test_fetch_and_persist_inserts_on_hit():
    fetcher, ingester, thread_builder = _mock_deps()
    fetcher.fetch_raw_by_id.return_value = _RAW_EMAIL

    n = fetch_and_persist(
        "test123@example.com", fetcher=fetcher, ingester=ingester,
        thread_builder=thread_builder, report=MagicMock(), quarantine=MagicMock(),
    )

    assert n == 1
    fetcher.fetch_raw_by_id.assert_called_once_with("test123@example.com")
    # parsed message routed through thread builder + upsert
    thread_builder.get_or_create_thread.assert_called_once()
    ingester._upsert_message.assert_called_once()
    # the parsed ParsedMessage carries the right message-id + thread_id
    pm = ingester._upsert_message.call_args[0][0]
    assert pm.message_id == "test123@example.com"
    assert ingester._upsert_message.call_args[0][1] == 42


def test_fetch_and_persist_returns_zero_on_404():
    fetcher, ingester, thread_builder = _mock_deps()
    fetcher.fetch_raw_by_id.return_value = b""  # not in lore archive

    n = fetch_and_persist(
        "missing@example.com", fetcher=fetcher, ingester=ingester,
        thread_builder=thread_builder, report=MagicMock(), quarantine=MagicMock(),
    )

    assert n == 0
    ingester._upsert_message.assert_not_called()
    thread_builder.get_or_create_thread.assert_not_called()


# ── thread-summary on-demand gating (ADR-025) ───────────────────────────────
# row = (message_count, summary_problem, summary_solution, summary_outcome)

@pytest.mark.parametrize("row,expected", [
    (None,                                     "none"),      # thread not found
    ((10, None, None, None),                   "none"),      # short → read raw
    ((29, "x", "y", "z"),                      "none"),      # just under threshold
    ((30, None, None, None),                   "generate"),  # at threshold, uncached
    ((50, None, None, None),                   "generate"),  # long, uncached
    ((50, "real problem", "sol", "out"),       "cached"),    # long, summarized
    ((50, "[summary-failed: timeout]", "", ""), "generate"), # stale failure → retry
])
def test_summary_decision(row, expected):
    assert _summary_decision(row) == expected
