"""Pricing primitives.

Under risk-neutral GBM with zero drift in the short tenor (zero-fee
collateralised contracts) the probability that S_T > K is

    P(S_T > K) = N( -d2 ),  d2 = (ln(S/K) - 0.5 * sigma^2 * T) / (sigma * sqrt(T))

This module implements:
- `prob_above` and its inverse `iv_from_prob` (used both ways for alpha)
- a *strike-monotonicity* check
- a *butterfly* / risk-neutral density check from a strip of strikes
- bid/ask-aware sum-to-one no-arb tests for binary and ternary

Returns are explicit dataclasses so the surface module can render them
without re-deriving anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

EPS_PROB = 1e-4


# ---------------------------------------------------------------------------
# GBM mapping  (price) <-> (prob_above)
# ---------------------------------------------------------------------------


def prob_above(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """Return P(S_T > K) under GBM with zero drift."""
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
    return float(norm.cdf(d2))


def iv_from_prob(prob: float, spot: float, strike: float, t_years: float) -> float | None:
    """Invert prob_above to recover the implied vol.

    Under zero-drift GBM:
      * ATM (S == K) is degenerate — prob_above ≤ 0.5 for every σ.
      * OTM (S < K) has prob_above non-monotonic in σ with a maximum at
        σ_max = sqrt(-2 ln(S/K) / T). We bracket on [ε, σ_max] and pick
        the increasing-branch solution (matches the perp's vol scale).
      * ITM (S > K) is monotonically decreasing in σ. We bracket on
        [ε, 20].

    Returns None when the quote is unreachable for any positive σ.
    """
    if t_years <= 0 or spot <= 0 or strike <= 0:
        return None
    p = max(min(prob, 1.0 - EPS_PROB), EPS_PROB)

    if abs(spot - strike) < 1e-6:
        return None

    if spot < strike:
        # OTM YES (call). σ_max where prob_above peaks.
        sigma_max = math.sqrt(max(0.0, -2.0 * math.log(spot / strike) / t_years))
        # Slightly inside the optimum so we are on the strictly-increasing branch.
        upper = max(1e-3, sigma_max * 0.999)
        peak_prob = prob_above(spot, strike, sigma_max, t_years)
        if p > peak_prob:
            return None
    else:
        # ITM YES — monotonically decreasing.
        upper = 20.0

    def f(sig: float) -> float:
        return prob_above(spot, strike, sig, t_years) - p

    try:
        return float(brentq(f, 1e-4, upper, maxiter=128))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Greeks of the digital P(S>K)  — used by the stat-arb engine
# ---------------------------------------------------------------------------


def digital_delta(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """∂P(S>K)/∂S = n(d2) / (S σ √T)."""
    if t_years <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
    return float(norm.pdf(d2) / (spot * sigma * math.sqrt(t_years)))


def digital_gamma(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """∂²P(S>K)/∂S². Closed form: -n(d2)/(S²σ√T) × (d2/(σ√T) + 1)."""
    if t_years <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    sq = sigma * math.sqrt(t_years)
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / sq
    return float(-norm.pdf(d2) / (spot * spot * sq) * (d2 / sq + 1.0))


def digital_vega(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """∂P(S>K)/∂σ = n(d2) × ∂d2/∂σ."""
    if t_years <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    sq = sigma * math.sqrt(t_years)
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / sq
    dd2_dsigma = -math.log(spot / strike) / (sigma * sq) - 0.5 * math.sqrt(t_years)
    return float(norm.pdf(d2) * dd2_dsigma)


def expected_carry_per_day(
    *, spot: float, strike: float, sigma_imp: float, sigma_rv: float, t_years: float
) -> float:
    """Delta-hedged digital P&L per day per unit notional.

    Standard BSM gamma carry: (½ · γ · S² · (σ_rv² − σ_imp²)) / 365 days.
    Sign convention: long the option (long YES) ⇒ positive when σ_rv > σ_imp.
    """
    if t_years <= 0 or sigma_imp <= 0 or sigma_rv <= 0:
        return 0.0
    g = digital_gamma(spot, strike, sigma_imp, t_years)
    return 0.5 * g * spot * spot * (sigma_rv * sigma_rv - sigma_imp * sigma_imp) / 365.0


# ---------------------------------------------------------------------------
# Bid/ask-aware no-arb checks
# ---------------------------------------------------------------------------


@dataclass
class BinaryEdge:
    """One BinaryMarket evaluated against perp anchor + own quotes."""

    target: float
    expiry_iso: str
    yes_mid: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_mid: float | None
    fair_yes: float | None             # GBM fair from perp anchor + sigma
    edge_bps: float | None             # (fair - mid) * 10_000
    parity_violation: float | None     # max(0, (yes_ask + no_ask) - 1) etc.
    arb_kind: str                      # "" / "long-yes" / "short-yes" / "parity"
    iv: float | None


def evaluate_binary(
    *,
    spot: float,
    sigma: float,
    target: float,
    expiry_iso: str,
    t_years: float,
    yes_mid: float | None,
    yes_bid: float | None,
    yes_ask: float | None,
    no_mid: float | None,
    no_bid: float | None,
    no_ask: float | None,
) -> BinaryEdge:
    fair = prob_above(spot, target, sigma, t_years) if spot and sigma else None

    edge = None
    if yes_mid is not None and fair is not None:
        edge = (fair - yes_mid) * 10_000

    iv = iv_from_prob(yes_mid, spot, target, t_years) if (yes_mid and spot) else None

    # Parity arb: Yes + No must equal 1
    parity = None
    kind = ""
    if yes_ask is not None and no_ask is not None:
        deficit = (yes_ask + no_ask) - 1.0
        # Long the cheaper side, short the dearer if (bid_yes + bid_no) > 1
        if yes_bid is not None and no_bid is not None:
            surplus = (yes_bid + no_bid) - 1.0
            if surplus > 0:
                parity = surplus
                kind = "sell-both"
            elif deficit < 0:
                parity = -deficit
                kind = "buy-both"

    if not kind and edge is not None:
        if edge > 50:
            kind = "long-yes"
        elif edge < -50:
            kind = "short-yes"

    return BinaryEdge(
        target=target,
        expiry_iso=expiry_iso,
        yes_mid=yes_mid,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_mid=no_mid,
        fair_yes=fair,
        edge_bps=edge,
        parity_violation=parity,
        arb_kind=kind,
        iv=iv,
    )


# ---------------------------------------------------------------------------
# Ternary simplex check
# ---------------------------------------------------------------------------


@dataclass
class TernaryEdge:
    k_low: float
    k_high: float
    expiry_iso: str
    p_down: float | None
    p_range: float | None
    p_up: float | None
    fair_down: float | None
    fair_range: float | None
    fair_up: float | None
    simplex_sum: float | None      # market p_down + p_range + p_up
    sum_violation: float | None    # |sum - 1|
    edge_down_bps: float | None
    edge_range_bps: float | None
    edge_up_bps: float | None
    arb_kind: str                  # "" / "buy-simplex" / "sell-simplex"


def evaluate_ternary(
    *,
    spot: float,
    sigma: float,
    k_low: float,
    k_high: float,
    expiry_iso: str,
    t_years: float,
    market_yes_low: float | None,    # P(S>K_low)
    market_yes_high: float | None,   # P(S>K_high)
    ask_yes_low: float | None,
    ask_no_low: float | None,
    ask_yes_high: float | None,
    ask_no_high: float | None,
) -> TernaryEdge:
    """Synthesise (P_down, P_range, P_up) from two binaries and grade."""

    # Market-implied simplex
    p_down = (1.0 - market_yes_low) if market_yes_low is not None else None
    p_up = market_yes_high
    p_range = None
    if market_yes_low is not None and market_yes_high is not None:
        p_range = market_yes_low - market_yes_high

    simplex_sum = None
    sum_violation = None
    if p_down is not None and p_range is not None and p_up is not None:
        simplex_sum = p_down + p_range + p_up
        sum_violation = simplex_sum - 1.0  # by construction ≈ 0 for binaries

    # Worst-case ask sum for a true ternary (matters for native priceTernary)
    arb_kind = ""
    if all(x is not None for x in (ask_no_low, ask_yes_high)):
        # Buy NO(K_low) + buy YES(K_high) + buy synthetic-range:
        # synthetic-range cost ≈ ask_yes_low + ask_no_high - 1
        cost_range = None
        if ask_yes_low is not None and ask_no_high is not None:
            cost_range = max(ask_yes_low + ask_no_high - 1.0, 0.0)
        if cost_range is not None:
            full_cost = ask_no_low + cost_range + ask_yes_high
            if full_cost < 1.0 - 1e-4:
                arb_kind = "buy-simplex"

    fair_down = fair_range = fair_up = None
    if spot and sigma and t_years > 0:
        fair_up = prob_above(spot, k_high, sigma, t_years)
        fair_below_low = 1.0 - prob_above(spot, k_low, sigma, t_years)
        fair_down = fair_below_low
        fair_range = max(0.0, 1.0 - fair_down - fair_up)

    def _bps(market: float | None, fair: float | None) -> float | None:
        if market is None or fair is None:
            return None
        return (fair - market) * 10_000

    return TernaryEdge(
        k_low=k_low,
        k_high=k_high,
        expiry_iso=expiry_iso,
        p_down=p_down,
        p_range=p_range,
        p_up=p_up,
        fair_down=fair_down,
        fair_range=fair_range,
        fair_up=fair_up,
        simplex_sum=simplex_sum,
        sum_violation=sum_violation,
        edge_down_bps=_bps(p_down, fair_down),
        edge_range_bps=_bps(p_range, fair_range),
        edge_up_bps=_bps(p_up, fair_up),
        arb_kind=arb_kind,
    )


# ---------------------------------------------------------------------------
# Butterfly density (RND) over a strike strip
# ---------------------------------------------------------------------------


@dataclass
class StripDensity:
    strikes: np.ndarray            # bin midpoints (length N-1)
    probs_above: np.ndarray        # original quotes (length N)
    cdf: np.ndarray                # 1 - probs_above (length N)
    pdf: np.ndarray                # forward-difference density (length N-1)
    pdf_negative_idx: list[int]    # bins flagged as butterfly arb


def butterfly_density(strikes: list[float], probs_above: list[float]) -> StripDensity:
    """Discrete RND from a strip of digitals at the same expiry.

    For digitals, P(S>K) IS the survival function; the density of the bin
    [K_i, K_{i+1}] is `(P_above[i] - P_above[i+1]) / (K_{i+1} - K_i)`.
    A negative bin density implies a butterfly arbitrage between the
    two strikes (the higher strike is more likely to be exceeded).
    """
    k = np.asarray(strikes, dtype=float)
    p = np.asarray(probs_above, dtype=float)
    order = np.argsort(k)
    k, p = k[order], p[order]
    cdf = 1.0 - p

    if len(k) < 2:
        return StripDensity(
            strikes=k, probs_above=p, cdf=cdf,
            pdf=np.array([]), pdf_negative_idx=[],
        )

    dk = np.diff(k)
    # P(K_i < S <= K_{i+1}) = P_above[i] - P_above[i+1] = cdf[i+1] - cdf[i]
    bin_prob = np.diff(cdf)
    pdf = bin_prob / np.where(dk > 0, dk, 1e-9)
    midpoints = 0.5 * (k[:-1] + k[1:])
    neg = [int(i) for i, v in enumerate(pdf) if v < -1e-6]
    return StripDensity(strikes=midpoints, probs_above=p, cdf=cdf, pdf=pdf, pdf_negative_idx=neg)
