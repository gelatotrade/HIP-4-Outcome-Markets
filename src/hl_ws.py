"""Hyperliquid WebSocket subscriber.

Replaces per-Dash-tick l2Book HTTP fan-out with a persistent connection
that streams updates for a fixed list of `coin` aliases. Updates land in
a thread-safe `BookCache` that the `Feed` reads synchronously.

Usage:
    cache = BookCache()
    runner = WSRunner(coins=["#100000", "#100001", "BTC", ...], cache=cache)
    runner.start()
    ...
    book = cache.get("#100000")           # latest L2Book
    mid  = cache.get_mid("BTC")           # latest perp mid

Reconnects automatically with exponential backoff. Caps per-host
connections by sharing one socket across all subscriptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import dataclass

from .hl_client import L2Book, L2Level
from .logging_config import get as get_logger

log = get_logger("hl_ws")

WS_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass
class _BookCacheEntry:
    book: L2Book
    received_at: float


class BookCache:
    """Thread-safe latest-snapshot store keyed by coin alias."""

    def __init__(self) -> None:
        self._books: dict[str, _BookCacheEntry] = {}
        self._mids: dict[str, float] = {}
        self._lock = threading.Lock()

    def update_book(self, coin: str, book: L2Book) -> None:
        with self._lock:
            self._books[coin] = _BookCacheEntry(book=book, received_at=time.time())

    def update_mid(self, coin: str, mid: float) -> None:
        with self._lock:
            self._mids[coin] = mid

    def get(self, coin: str) -> L2Book | None:
        with self._lock:
            entry = self._books.get(coin)
            return entry.book if entry else None

    def get_mid(self, coin: str) -> float | None:
        with self._lock:
            return self._mids.get(coin)

    def all_books(self) -> dict[str, L2Book]:
        with self._lock:
            return {c: e.book for c, e in self._books.items()}

    def all_mids(self) -> dict[str, float]:
        with self._lock:
            return dict(self._mids)

    def age_seconds(self, coin: str) -> float | None:
        with self._lock:
            entry = self._books.get(coin)
            return (time.time() - entry.received_at) if entry else None


class WSRunner:
    """Owns a background thread that runs the asyncio websocket loop."""

    def __init__(self, coins: list[str], cache: BookCache, url: str = WS_URL) -> None:
        self.coins = list(coins)
        self.cache = cache
        self.url = url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hl-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:                                # noqa: BLE001
            log.error("ws.runner_crashed", err=str(exc))

    async def _main(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except Exception as exc:                            # noqa: BLE001
                log.warning("ws.session_error", err=str(exc), backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        import websockets

        async with websockets.connect(self.url, ping_interval=20, ping_timeout=10) as sock:
            log.info("ws.connected", coins=len(self.coins))
            for coin in self.coins:
                await sock.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": coin},
                }))
            await sock.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "allMids"},
            }))

            while not self._stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    msg = await asyncio.wait_for(sock.recv(), timeout=1.0)
                    self._handle(msg)

    def _handle(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        channel = data.get("channel")
        body = data.get("data") or {}
        if channel == "l2Book":
            coin = body.get("coin")
            levels = body.get("levels")
            if not coin or not levels or len(levels) < 2:
                return
            bids = [L2Level(px=float(r["px"]), sz=float(r["sz"])) for r in levels[0][:5]]
            asks = [L2Level(px=float(r["px"]), sz=float(r["sz"])) for r in levels[1][:5]]
            self.cache.update_book(coin, L2Book(
                coin=coin, bids=bids, asks=asks,
                ts_ms=int(body.get("time", time.time() * 1000)),
            ))
        elif channel == "allMids":
            mids = body.get("mids") or {}
            for coin, px in mids.items():
                try:
                    self.cache.update_mid(coin, float(px))
                except (TypeError, ValueError):
                    continue
