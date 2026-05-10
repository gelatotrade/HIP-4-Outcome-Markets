"""Glue between HLClient / simulator / CSV-replay and the strategy layer.

`Feed.snapshot()` runs in three modes (selected at construction):
    1. live      — calls api.hyperliquid.xyz
    2. csv       — replays a CSV captured by `scripts/fetch_hl.py`
    3. simulated — synthetic drifting universe (no network)

`Feed.history` keeps the last `history_len` snapshots so the dashboard
can scrub through time and animate the alpha surface.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .contracts import BinaryMarket, TernaryMarket, assemble_binary_markets, synthesise_ternaries
from .data_loader import CSVReplay
from .hl_client import HLClient, L2Book
from .pricing import (
    BinaryEdge,
    StripDensity,
    TernaryEdge,
    butterfly_density,
    evaluate_binary,
    evaluate_ternary,
)
from .simulator import synthetic_universe
from .statarb import StatArbResult, evaluate_book

log = logging.getLogger(__name__)

REALIZED_VOL_DEFAULT = 0.65
HISTORY_DEFAULT = 240


@dataclass
class MarketSnapshot:
    ts: float
    spot: float
    sigma: float                              # realised vol estimate
    binaries: list[BinaryMarket]
    ternaries: list[TernaryMarket]
    binary_edges: list[BinaryEdge]
    ternary_edges: list[TernaryEdge]
    statarb: StatArbResult                    # IV-RV positions + carry
    densities: dict[str, StripDensity] = field(default_factory=dict)
    source: str = "live"
    error: str | None = None


class Feed:
    def __init__(
        self,
        *,
        allow_live: bool = True,
        csv_path: str | None = None,
        vol_window_s: float = 1800.0,
        history_len: int = HISTORY_DEFAULT,
        threshold_vol: float = 0.05,
        hedge_ratio: float = 1.0,
    ) -> None:
        self._client = HLClient() if allow_live and not csv_path else None
        self._csv = CSVReplay(csv_path) if csv_path else None
        self._spot_history: list[tuple[float, float]] = []
        self.history: deque[MarketSnapshot] = deque(maxlen=history_len)
        self.vol_window_s = vol_window_s
        self.threshold_vol = threshold_vol
        self.hedge_ratio = hedge_ratio
        self._last_snapshot: MarketSnapshot | None = None

    # -- knobs settable from the dashboard -------------------------------

    def set_params(self, *, vol_window_s: float | None = None,
                   threshold_vol: float | None = None,
                   hedge_ratio: float | None = None) -> None:
        if vol_window_s is not None:
            self.vol_window_s = vol_window_s
        if threshold_vol is not None:
            self.threshold_vol = threshold_vol
        if hedge_ratio is not None:
            self.hedge_ratio = hedge_ratio

    # -- vol estimate ----------------------------------------------------

    def _push_spot(self, spot: float) -> None:
        self._spot_history.append((time.time(), spot))
        cutoff = time.time() - 6 * 3600
        self._spot_history = [(t, p) for t, p in self._spot_history if t >= cutoff]

    def _realized_vol(self) -> float:
        cutoff = time.time() - self.vol_window_s
        rows = [(t, p) for t, p in self._spot_history if t >= cutoff]
        if len(rows) < 12:
            return REALIZED_VOL_DEFAULT
        times = np.array([t for t, _ in rows])
        prices = np.array([p for _, p in rows])
        log_rets = np.diff(np.log(prices))
        dts = np.diff(times)
        with np.errstate(divide="ignore", invalid="ignore"):
            inst = log_rets / np.sqrt(np.where(dts > 0, dts, 1.0))
        if not np.isfinite(inst).any():
            return REALIZED_VOL_DEFAULT
        return float(np.sqrt(365.25 * 24 * 3600) * np.nanstd(inst))

    # -- main entry ------------------------------------------------------

    def snapshot(self) -> MarketSnapshot:
        assets, books, spot = [], {}, None
        source = "live"
        error: str | None = None

        if self._csv is not None:
            assets, books, spot = self._csv.next_tick()
            source = "csv"
        elif self._client is not None:
            try:
                assets = self._client.outcome_meta()
                if assets:
                    for a in assets:
                        b = self._client.l2_book(a.coin)
                        if b is not None:
                            books[a.coin] = b
                spot = self._client.perp_mid("BTC")
            except Exception as exc:                                # noqa: BLE001
                error = f"live fetch failed: {exc}"
                log.warning(error)

        had_live_assets = bool(assets)
        had_live_spot = spot is not None
        if not assets or spot is None:
            sim_assets, sim_books, sim_mids = synthetic_universe()
            if not had_live_assets:
                assets, books = sim_assets, sim_books
            if not had_live_spot:
                spot = sim_mids["BTC"]
            if self._csv is not None:
                source = "csv-fallback"
            else:
                source = "simulated" if not (had_live_assets or had_live_spot) else "partial"
            if self._client is not None and self._client.last_error:
                error = self._client.last_error

        self._push_spot(spot)
        sigma = self._realized_vol()

        binaries = assemble_binary_markets(assets)
        for b in binaries:
            b.yes_book = books.get(b.yes.coin)
            b.no_book = books.get(b.no.coin)
        ternaries = synthesise_ternaries(binaries)

        binary_edges = [
            evaluate_binary(
                spot=spot, sigma=sigma, target=b.target,
                expiry_iso=b.expiry.isoformat(), t_years=b.t_to_expiry_years,
                yes_mid=b.yes_mid, yes_bid=b.yes_bid, yes_ask=b.yes_ask,
                no_mid=b.no_mid, no_bid=b.no_bid, no_ask=b.no_ask,
            )
            for b in binaries
        ]

        ternary_edges = []
        for t in ternaries:
            if t.down is None or t.up is None:
                continue
            ternary_edges.append(evaluate_ternary(
                spot=spot, sigma=sigma, k_low=t.k_low, k_high=t.k_high,
                expiry_iso=t.expiry.isoformat(), t_years=t.t_to_expiry_years,
                market_yes_low=t.down.yes_mid, market_yes_high=t.up.yes_mid,
                ask_yes_low=t.down.yes_ask, ask_no_low=t.down.no_ask,
                ask_yes_high=t.up.yes_ask, ask_no_high=t.up.no_ask,
            ))

        densities: dict[str, StripDensity] = {}
        by_expiry: dict[tuple[str, str], list[BinaryMarket]] = {}
        for b in binaries:
            if b.yes_mid is None:
                continue
            by_expiry.setdefault((b.underlying, b.expiry.isoformat()), []).append(b)
        for (under, exp_iso), strip in by_expiry.items():
            if len(strip) < 2:
                continue
            ks = [b.target for b in strip]
            ps = [b.yes_mid for b in strip if b.yes_mid is not None]
            if len(ks) == len(ps):
                densities[f"{under}@{exp_iso}"] = butterfly_density(ks, ps)

        statarb = evaluate_book(
            spot=spot, sigma_rv=sigma, binaries=binaries,
            threshold=self.threshold_vol, hedge_ratio=self.hedge_ratio,
        )

        snap = MarketSnapshot(
            ts=time.time(), spot=spot, sigma=sigma,
            binaries=binaries, ternaries=ternaries,
            binary_edges=binary_edges, ternary_edges=ternary_edges,
            statarb=statarb, densities=densities,
            source=source, error=error,
        )
        self._last_snapshot = snap
        self.history.append(snap)
        return snap

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
