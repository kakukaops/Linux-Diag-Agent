"""Unit tests for llm/rate_limit/sliding_window.py."""

import time
import pytest
from llm.rate_limit.sliding_window import SlidingWindowRateLimiter


def test_basic_acquire():
    limiter = SlidingWindowRateLimiter(window_seconds=60, max_calls=5)
    for _ in range(5):
        assert limiter.acquire(block=False) is True
    # 6th should fail (non-blocking)
    assert limiter.acquire(block=False) is False


def test_remaining():
    limiter = SlidingWindowRateLimiter(window_seconds=60, max_calls=3)
    assert limiter.remaining() == 3
    limiter.acquire(block=False)
    assert limiter.remaining() == 2


def test_window_eviction():
    limiter = SlidingWindowRateLimiter(window_seconds=0.05, max_calls=2)
    assert limiter.acquire(block=False) is True
    assert limiter.acquire(block=False) is True
    assert limiter.acquire(block=False) is False
    time.sleep(0.1)
    # After window passes, slots become available
    assert limiter.acquire(block=False) is True


def test_reset_seconds_when_full():
    limiter = SlidingWindowRateLimiter(window_seconds=10, max_calls=1)
    limiter.acquire(block=False)
    reset = limiter.reset_seconds()
    assert 0 < reset <= 10
