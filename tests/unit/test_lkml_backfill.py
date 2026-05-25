"""Unit tests for LKML reference-driven backfill (ADR-025 L1).

Covers the pure logic (message-id validation, checkpoint round-trip) and the
fetch-by-id URL construction. The full backfill loop is an integration test
(needs DB + lore network) and is intentionally not exercised here.
"""

import json
from unittest.mock import MagicMock

import pytest

from ingest.lkml.backfill import (
    _is_valid_message_id,
    _load_checkpoint,
    _save_checkpoint,
)
from ingest.lkml.fetcher import LoreFetcher


# ── message-id validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("mid", [
    "20260413213335.4010d8f2@pumpkin",
    "0-v1-20700abdf239+19c-iommu_no_dma_iommu_jgg@nvidia.com",
    "20260518115553.3513034-1-usama.arif@linux.dev",
])
def test_valid_message_ids(mid):
    assert _is_valid_message_id(mid)


@pytest.mark.parametrize("mid", [
    "",                       # empty
    "no-at-sign-here",        # missing '@'
    "has space@host.com",     # whitespace
    "a@b",                    # too short
    None,                     # None
])
def test_invalid_message_ids(mid):
    assert not _is_valid_message_id(mid or "")


# ── checkpoint round-trip ───────────────────────────────────────────────────

def test_checkpoint_roundtrip(tmp_path):
    ckpt = {"fetched": ["a@b.com", "c@d.com"], "not_found": ["x@y.com"],
            "last_run": "2026-05-21T00:00:00+00:00"}
    _save_checkpoint(tmp_path, ckpt)
    loaded = _load_checkpoint(tmp_path)
    assert loaded["fetched"] == ["a@b.com", "c@d.com"]
    assert loaded["not_found"] == ["x@y.com"]
    assert loaded["last_run"] == "2026-05-21T00:00:00+00:00"


def test_checkpoint_missing_returns_empty(tmp_path):
    loaded = _load_checkpoint(tmp_path)
    assert loaded == {"fetched": [], "not_found": [], "last_run": None}


def test_checkpoint_corrupt_returns_empty(tmp_path):
    (tmp_path / "lkml_backfill_checkpoint.json").write_text("{not json")
    loaded = _load_checkpoint(tmp_path)
    assert loaded["fetched"] == []


def test_checkpoint_partial_keys_filled(tmp_path):
    """An old checkpoint missing newer keys is normalised on load."""
    (tmp_path / "lkml_backfill_checkpoint.json").write_text('{"fetched": ["m@h"]}')
    loaded = _load_checkpoint(tmp_path)
    assert loaded["fetched"] == ["m@h"]
    assert loaded["not_found"] == []
    assert loaded["last_run"] is None


# ── fetch-by-id URL construction (mocked transport) ─────────────────────────

def test_fetch_raw_by_id_uses_all_archive(tmp_path):
    """fetch_raw_by_id must address the list-agnostic /all/ archive."""
    fetcher = LoreFetcher(tmp_path)
    captured = {}

    def fake_fetch_raw(permalink):
        captured["permalink"] = permalink
        return b"raw-bytes"

    fetcher._fetch_raw = fake_fetch_raw  # type: ignore[assignment]
    out = fetcher.fetch_raw_by_id("<20260413.abc@host>")

    assert out == b"raw-bytes"
    # angle brackets stripped; routed through /all/
    assert captured["permalink"] == "https://lore.kernel.org/all/20260413.abc@host"
    fetcher.close()


def test_fetch_raw_by_id_keeps_special_chars(tmp_path):
    """'+' / '_' / '.' / '-' in message-ids must survive into the path."""
    fetcher = LoreFetcher(tmp_path)
    captured = {}
    fetcher._fetch_raw = lambda p: captured.setdefault("p", p) or b""  # type: ignore
    fetcher.fetch_raw_by_id("0-v1-abc+19c-iommu_jgg@nvidia.com")
    assert captured["p"].endswith("/all/0-v1-abc+19c-iommu_jgg@nvidia.com")
    fetcher.close()
