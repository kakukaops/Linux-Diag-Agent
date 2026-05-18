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
    """Thread-safe sliding window rate limiter.

    Args:
        window_seconds: Length of the rolling window (e.g. 18000 for 5h).
        max_calls: Maximum calls allowed within the window.
    """

    def __init__(self, window_seconds: float = 18000, max_calls: int = 40) -> None:
        self._window = window_seconds
        self._max = max_calls
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, block: bool = True) -> bool:
        """Block until a call slot is available, or return False immediately.

        Returns True when a slot is acquired, False if block=False and no slot.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                self._evict(now)
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return True
                if not block:
                    return False
                # Compute wait until the oldest entry expires
                wait = self._timestamps[0] + self._window - now

            time.sleep(max(0.1, wait))

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
