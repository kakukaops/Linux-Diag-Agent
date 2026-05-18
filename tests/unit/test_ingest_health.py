"""Unit tests for ingestion health monitor (WBS 11.8)."""

import pytest
from datetime import datetime, timedelta, timezone
from ingest.health import IngesterHealth, STALE_DAYS, CIRCUIT_OPEN_AFTER


def _health(consecutive_errors: int, last_success_days_ago: int | None = None) -> IngesterHealth:
    h = IngesterHealth(source="test")
    h.consecutive_errors = consecutive_errors
    if last_success_days_ago is not None:
        h.last_success = datetime.now(timezone.utc) - timedelta(days=last_success_days_ago)
    return h


def test_is_stale_with_no_success():
    h = IngesterHealth(source="test")
    assert h.is_stale is True


def test_is_stale_old_success():
    h = _health(0, last_success_days_ago=STALE_DAYS + 1)
    assert h.is_stale is True


def test_not_stale_recent_success():
    h = _health(0, last_success_days_ago=1)
    assert h.is_stale is False


def test_circuit_open_after_threshold():
    h = _health(CIRCUIT_OPEN_AFTER)
    h.circuit_open = True
    h.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert not h.can_run


def test_circuit_closes_after_cooldown():
    h = _health(CIRCUIT_OPEN_AFTER)
    h.circuit_open = True
    h.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert h.can_run  # cooldown expired


def test_no_circuit_below_threshold():
    h = _health(CIRCUIT_OPEN_AFTER - 1)
    assert h.can_run  # circuit not open
