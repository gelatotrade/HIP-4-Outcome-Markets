"""Token-bucket rate limiter sized for Hyperliquid's REST limits.

Hyperliquid public info endpoint: ~100 req/min sustained, with bursts.
We default to 90/min steady to leave headroom; configurable per-instance
for the executor's separate /exchange budget.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Thread-safe token bucket. `acquire(n)` blocks until n tokens fit."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else max(rate_per_sec, 1.0))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        dt = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + dt * self.rate)
        self._last = now

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: int = 1, *, max_wait_s: float | None = None) -> bool:
        deadline = None if max_wait_s is None else time.monotonic() + max_wait_s
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                deficit = n - self._tokens
                wait_s = deficit / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_s = min(wait_s, remaining)
            time.sleep(min(wait_s, 0.5))


# Sensible defaults shared by HLClient.
INFO_LIMITER = TokenBucket(rate_per_sec=90 / 60, capacity=20)
EXCHANGE_LIMITER = TokenBucket(rate_per_sec=10, capacity=20)
