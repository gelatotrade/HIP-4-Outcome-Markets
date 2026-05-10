"""Statistical arbitrage engine for HIP-4 outcome contracts.

Thesis
------
HIP-4 outcome markets are settled digitals. Their mid-price is the
risk-neutral probability `P(S_T > K)`. Inverting that probability gives
an implied volatility `σ_imp`. Compared to a realised-vol estimate
`σ_rv` extracted from the BTC perpetual tape, gaps are an exploitable
edge:

    edge_vol_pts = σ_imp − σ_rv

A delta-hedged digital book harvests the gamma carry

    expected_pnl/day  =  ½ · γ · S² · (σ_rv² − σ_imp²) / 365

For each market we therefore:
  1. Compute σ_imp from the YES mid.
  2. Compute σ_rv from the perp tape (already cached on the feed).
  3. If |edge| above threshold, mark a position (+1 long YES, −1 short YES)
     and a delta-hedge size in BTC perp.
  4. Aggregate per-tick theoretical P&L into a cumulative alpha series
     so the dashboard can plot how alpha is generated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import BinaryMarket
from .pricing import (
    digital_delta,
    digital_gamma,
    digital_vega,
    expected_carry_per_day,
    iv_from_prob,
)

VOL_EDGE_THRESHOLD = 0.05   # 5 vol points before we take a position
NOTIONAL_PER_LEG = 1_000.0   # USD notional for sizing


@dataclass
class Signal:
    target: float
    expiry_iso: str
    t_years: float
    mid: float | None
    iv: float | None
    rv: float
    edge_vol: float | None         # iv − rv
    delta: float
    gamma: float
    vega: float
    direction: int                 # +1 long YES, −1 short YES, 0 flat
    perp_hedge_units: float        # BTC perp position to delta-neutralise
    expected_pnl_day: float        # USD per NOTIONAL_PER_LEG


@dataclass
class StatArbResult:
    ts: float
    spot: float
    sigma_rv: float
    signals: list[Signal] = field(default_factory=list)
    expected_pnl_day_total: float = 0.0
    n_active: int = 0


def evaluate_book(
    *,
    spot: float,
    sigma_rv: float,
    binaries: list[BinaryMarket],
    threshold: float = VOL_EDGE_THRESHOLD,
    hedge_ratio: float = 1.0,
    notional_per_leg: float = NOTIONAL_PER_LEG,
) -> StatArbResult:
    out = StatArbResult(ts=datetime.now(timezone.utc).timestamp(), spot=spot, sigma_rv=sigma_rv)
    for b in binaries:
        mid = b.yes_mid
        T = b.t_to_expiry_years
        if mid is None or T <= 0:
            continue
        iv = iv_from_prob(mid, spot, b.target, T)
        if iv is None:
            continue
        edge = iv - sigma_rv

        direction = 0
        if edge < -threshold:
            direction = +1   # IV cheap → buy YES, expect σ_rv > σ_imp
        elif edge > threshold:
            direction = -1   # IV rich → sell YES

        delta = digital_delta(spot, b.target, iv, T)
        gamma = digital_gamma(spot, b.target, iv, T)
        vega = digital_vega(spot, b.target, iv, T)
        # delta-hedge size: position pays $1 per contract; one "contract" = $1 notional
        # so delta in $/$ × notional × hedge_ratio gives BTC notional we need to short
        perp_hedge_usd = direction * delta * notional_per_leg * hedge_ratio
        perp_hedge_units = -perp_hedge_usd / spot if spot > 0 else 0.0

        carry_pos = expected_carry_per_day(
            spot=spot, strike=b.target, sigma_imp=iv, sigma_rv=sigma_rv, t_years=T,
        )
        # carry sign convention: long-vol P&L per dollar notional. Multiply by direction
        # because direction=+1 means long YES (long vol when γ>0 OTM). Engine convention
        # captured by direction × carry_per_day:
        expected = direction * carry_pos * notional_per_leg

        out.signals.append(
            Signal(
                target=b.target, expiry_iso=b.expiry.isoformat(), t_years=T,
                mid=mid, iv=iv, rv=sigma_rv, edge_vol=edge,
                delta=delta, gamma=gamma, vega=vega,
                direction=direction, perp_hedge_units=perp_hedge_units,
                expected_pnl_day=expected,
            )
        )
        if direction != 0:
            out.expected_pnl_day_total += expected
            out.n_active += 1
    return out
