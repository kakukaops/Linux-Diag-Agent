"""Pro-subscription sliding window rate limiter.

Tracks call timestamps within a fixed window (default 5h / 18000s).
Blocks (does not discard) when the limit is reached, resuming when
the oldest call falls outside the window.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter with optional minimum inter-call interval.

    Args:
        window_seconds: Length of the rolling window (e.g. 18000 for 5h).
        max_calls: Maximum calls allowed within the window.
        min_interval_seconds: Minimum time between successive calls (0 = no spacing).
            Set to window_seconds/max_calls to spread calls evenly and avoid burst.
    """

    def __init__(self, window_seconds: float = 18000, max_calls: int = 40,
                 min_interval_seconds: float = 0.0) -> None:
        self._window = window_seconds
        self._max = max_calls
        self._min_interval = min_interval_seconds
        self._timestamps: deque[float] = deque()
        self._last_acquired: float = 0.0
        self._lock = threading.Lock()

    def acquire(self, block: bool = True) -> bool:
        """Block until a call slot is available, or return False immediately.

        Returns True when a slot is acquired, False if block=False and no slot.
        Enforces both the sliding-window cap and the minimum inter-call interval.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                self._evict(now)
                window_ok = len(self._timestamps) < self._max
                interval_wait = max(0.0, self._last_acquired + self._min_interval - now)
                if window_ok and interval_wait <= 0:
                    self._timestamps.append(now)
                    self._last_acquired = now
                    return True
                if not block:
                    return False
                if not window_ok:
                    wait = self._timestamps[0] + self._window - now
                else:
                    wait = interval_wait

            time.sleep(max(0.05, wait))

    def remaining(self) -> int:
        with self._lock:
            self._evict(time.monotonic())
            return max(0, self._max - len(self._timestamps))

    def reset_seconds(self) -> float:
        """Seconds until the window resets enough to allow one more call."""
        with self._lock:
            if not self._timestamps:
                return 0.0
            now = time.monotonic()
            self._evict(now)
            if len(self._timestamps) < self._max:
                return 0.0
            return max(0.0, self._timestamps[0] + self._window - now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


# Per-role global instances (shared across all provider instantiations)
_limiter_registry: dict[str, SlidingWindowRateLimiter] = {}
_registry_lock = threading.Lock()


def get_limiter(role: str = "chat") -> SlidingWindowRateLimiter:
    """Return the shared rate limiter for the given role."""
    with _registry_lock:
        if role not in _limiter_registry:
            from configs.config import get_config
            cfg = get_config()
            role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
            rl = role_cfg.rate_limit
            _limiter_registry[role] = SlidingWindowRateLimiter(
                window_seconds=rl.window_seconds,
                max_calls=rl.max_messages,
            )
        return _limiter_registry[role]


# Per-endpoint global instances — shared across ALL roles using the same API endpoint.
# This enforces account-level RPM caps (e.g. OpenRouter 20 rpm total, not per-role).
_endpoint_limiters: dict[str, SlidingWindowRateLimiter] = {}
_endpoint_lock = threading.Lock()


def get_endpoint_limiter(endpoint: str, rpm: int) -> SlidingWindowRateLimiter:
    """Return the shared per-minute rate limiter for the given API endpoint URL.

    Uses min_interval_seconds = 60/rpm to spread calls evenly and avoid burst
    throttling from upstream providers even when within the per-minute cap.
    First call for an endpoint wins; subsequent calls share the same limiter.
    """
    with _endpoint_lock:
        if endpoint not in _endpoint_limiters:
            min_interval = 60.0 / rpm if rpm > 0 else 0.0
            _endpoint_limiters[endpoint] = SlidingWindowRateLimiter(
                window_seconds=60,
                max_calls=rpm,
                min_interval_seconds=min_interval,
            )
        return _endpoint_limiters[endpoint]
