"""TokenBucket tests."""

from __future__ import annotations

import threading
import time

from src.rate_limit import TokenBucket


def test_initial_bucket_full():
    b = TokenBucket(rate_per_sec=10, capacity=5)
    for _ in range(5):
        assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_refill_over_time():
    b = TokenBucket(rate_per_sec=100, capacity=2)
    assert b.try_acquire(2) is True
    assert b.try_acquire() is False
    time.sleep(0.06)               # 6 tokens worth
    assert b.try_acquire() is True


def test_acquire_blocks_until_available():
    b = TokenBucket(rate_per_sec=20, capacity=1)
    assert b.try_acquire() is True
    t0 = time.monotonic()
    assert b.acquire(1, max_wait_s=1.0) is True
    elapsed = time.monotonic() - t0
    assert 0.03 < elapsed < 0.5    # 1 token at 20/s = 50 ms ± slack


def test_acquire_times_out():
    b = TokenBucket(rate_per_sec=0.1, capacity=1)  # very slow
    assert b.try_acquire() is True
    assert b.acquire(1, max_wait_s=0.05) is False


def test_thread_safety():
    b = TokenBucket(rate_per_sec=10_000, capacity=100)
    granted = [0]
    lock = threading.Lock()

    def worker() -> None:
        n = 0
        for _ in range(50):
            if b.try_acquire():
                n += 1
        with lock:
            granted[0] += n

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    # 10 workers * 50 = 500 attempts; bucket cap is 100 plus some refill.
    assert granted[0] <= 200
    assert granted[0] >= 100
