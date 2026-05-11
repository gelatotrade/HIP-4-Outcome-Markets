"""Glue between the StatArb engine output and the Rust execution daemon.

`SignalEmitter` consumes `MarketSnapshot`s from the feed, derives the
positions the engine wants opened *right now*, deduplicates against the
last emission window, and pushes them as `Signal` NDJSON over the UDS.

Defaults to OFF: the dashboard runs read-only unless
`EXECUTOR_ENABLE_TRADING=1` is set. Even when on, it remains a thin
glue layer — risk gating happens in the Rust executor.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .executor_client import ExecutorClient, Signal
from .feed import MarketSnapshot
from .logging_config import get as get_logger

log = get_logger("signal_emitter")

# Hyperliquid spot tick for outcome contracts is 0.001 (probability).
PRICE_DP = 4
# Default slippage when sending the hedge IOC at perp mid.
DEFAULT_SLIPPAGE_BPS = 50
# How long after first emission can we re-emit the same (asset, side) pair?
DEDUP_WINDOW_SECONDS = 30.0


@dataclass(frozen=True)
class _DedupKey:
    outcome_asset: int
    side: str           # "Y" / "N"


class SignalEmitter:
    """Emit one signal per active leg per dedup window.

    Lifecycle:
        emitter = SignalEmitter()
        for snap in feed:
            emitter.on_snapshot(snap)
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        client: ExecutorClient | None = None,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        ttl_ms: int = 2_500,
        dedup_window_s: float = DEDUP_WINDOW_SECONDS,
        btc_perp_asset_id: int | None = None,
    ) -> None:
        env_enabled = os.environ.get("EXECUTOR_ENABLE_TRADING", "").lower() in {
            "1", "true", "yes", "on"}
        self.enabled = env_enabled if enabled is None else enabled
        self.slippage_bps = slippage_bps
        self.ttl_ms = ttl_ms
        self.dedup_window_s = dedup_window_s
        self._btc_perp_asset_id = (
            btc_perp_asset_id
            if btc_perp_asset_id is not None
            else int(os.environ.get("BTC_PERP_ASSET_ID", "0"))
        )
        self._client: ExecutorClient | None = (
            client if client is not None else (ExecutorClient() if self.enabled else None)
        )
        self._last_emit: dict[_DedupKey, float] = {}
        self.emitted_count = 0
        self.suppressed_count = 0

    # ----------------------------------------------------------------------

    def _coin_to_asset_id(self, coin: str) -> int | None:
        """Outcome assets are addressed by their numeric id (the `#N` suffix).

        Hyperliquid's `outcomeMeta` may return either the bare numeric id or
        the `#N` alias; we tolerate both.
        """
        s = coin.lstrip("#@")
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    def on_snapshot(self, snap: MarketSnapshot) -> int:
        """Return number of signals actually emitted for this snapshot."""
        if not self.enabled or self._client is None:
            return 0

        now = time.time()
        emitted = 0
        for sig in snap.statarb.signals:
            if sig.direction == 0:
                continue
            asset_id = self._asset_id_for_signal(snap, sig)
            if asset_id is None:
                continue
            side = "Y" if sig.direction == +1 else "N"
            key = _DedupKey(outcome_asset=asset_id, side=side)
            last = self._last_emit.get(key)
            if last is not None and (now - last) < self.dedup_window_s:
                self.suppressed_count += 1
                continue
            if sig.mid is None or snap.spot <= 0:
                continue

            # When LONG-YES (direction +1) we BUY YES at its ask; when SHORT
            # we SELL YES at its bid. We approximate with mid here — the Rust
            # executor will still IOC-cross the book.
            px_str = f"{sig.mid:.{PRICE_DP}f}"

            wire = Signal(
                id=f"{int(now*1000)}-{asset_id}-{side}",
                kind="open",
                outcome_asset=asset_id,
                side=side,
                px=px_str,
                notional_usd=float(snap.statarb.gross_notional_usd
                                   / max(snap.statarb.n_active, 1)),
                perp_delta_btc=float(sig.perp_hedge_units),
                perp_asset=self._btc_perp_asset_id,
                perp_ref_px=float(snap.spot),
                slippage_bps=self.slippage_bps,
                ts_ms=int(now * 1000),
                ttl_ms=self.ttl_ms,
            )
            try:
                self._client.send(wire)
                self._last_emit[key] = now
                self.emitted_count += 1
                emitted += 1
                log.info("signal.emitted", id=wire.id, asset=asset_id,
                         side=side, dir=sig.direction,
                         notional=wire.notional_usd,
                         hedge_btc=wire.perp_delta_btc)
            except OSError as exc:
                log.warning("signal.emit_failed", err=str(exc))
                break  # socket dead, retry on the next snapshot
        return emitted

    def _asset_id_for_signal(self, snap: MarketSnapshot, sig) -> int | None:
        """Map a statarb signal back to its on-chain outcome asset id."""
        for binary in snap.binaries:
            if binary.target != sig.target:
                continue
            if binary.expiry.isoformat() != sig.expiry_iso:
                continue
            coin = binary.yes.coin if sig.direction == +1 else binary.no.coin
            return self._coin_to_asset_id(coin) or binary.yes.asset_id
        return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
