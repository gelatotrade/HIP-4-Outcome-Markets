"""Tests for the IV-vs-RV stat-arb engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.contracts import BinaryMarket
from src.hl_client import L2Book, L2Level, OutcomeAsset
from src.statarb import evaluate_book


def _binary(target: float, yes_mid: float) -> BinaryMarket:
    expiry = datetime.now(UTC) + timedelta(days=1)
    yes = OutcomeAsset(asset_id=1, coin="#1", name="Y", side="Y", outcome_id=0, description="")
    no = OutcomeAsset(asset_id=2, coin="#2", name="N", side="N", outcome_id=0, description="")
    half = 0.005
    book_y = L2Book(coin="#1",
                    bids=[L2Level(px=yes_mid - half, sz=10)],
                    asks=[L2Level(px=yes_mid + half, sz=10)], ts_ms=0)
    book_n = L2Book(coin="#2",
                    bids=[L2Level(px=1 - yes_mid - half, sz=10)],
                    asks=[L2Level(px=1 - yes_mid + half, sz=10)], ts_ms=0)
    bm = BinaryMarket(underlying="BTC", target=target, expiry=expiry,
                      period="1d", yes=yes, no=no, yes_book=book_y, no_book=book_n)
    return bm


def _quote_for_iv(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """Helper: build a market quote that inverts to a known σ_imp."""
    from src.pricing import prob_above
    return prob_above(spot, strike, sigma, t_years)


def test_long_yes_when_iv_below_rv():
    # Quote built from σ_imp = 0.40, σ_RV = 0.80 ⇒ engine LONGs YES
    spot, strike, t_years = 80_000, 82_000, 1 / 365
    mid = _quote_for_iv(spot, strike, 0.40, t_years)
    bm = _binary(strike, mid)
    res = evaluate_book(spot=spot, sigma_rv=0.80, binaries=[bm],
                        threshold=0.05, hedge_ratio=1.0)
    assert res.signals
    sig = res.signals[0]
    assert sig.direction == +1, "expected long-YES when σ_imp < σ_RV"
    assert sig.perp_hedge_units < 0, "long YES → hedge SHORT BTC"


def test_short_yes_when_iv_above_rv():
    spot, strike, t_years = 80_000, 82_000, 1 / 365
    mid = _quote_for_iv(spot, strike, 0.90, t_years)
    bm = _binary(strike, mid)
    res = evaluate_book(spot=spot, sigma_rv=0.30, binaries=[bm],
                        threshold=0.05, hedge_ratio=1.0)
    assert res.signals
    sig = res.signals[0]
    assert sig.direction == -1, "expected short-YES when σ_imp > σ_RV"
    assert sig.perp_hedge_units > 0, "short YES → hedge LONG BTC"


def test_no_signal_when_within_threshold():
    spot, strike, t_years = 80_000, 82_000, 1 / 365
    mid = _quote_for_iv(spot, strike, 0.65, t_years)
    bm = _binary(strike, mid)
    res = evaluate_book(spot=spot, sigma_rv=0.65, binaries=[bm],
                        threshold=0.50, hedge_ratio=1.0)
    assert res.signals[0].direction == 0


def test_carry_sign_matches_direction():
    spot, strike, t_years = 80_000, 82_000, 1 / 365
    mid = _quote_for_iv(spot, strike, 0.40, t_years)   # cheap vol
    bm = _binary(strike, mid)
    res = evaluate_book(spot=spot, sigma_rv=1.0, binaries=[bm],
                        threshold=0.05, hedge_ratio=1.0,
                        notional_per_leg=10_000.0)
    sig = next(s for s in res.signals if s.direction != 0)
    assert sig.expected_pnl_day > 0


def test_hedge_ratio_scales_perp_position():
    spot, strike, t_years = 80_000, 82_000, 1 / 365
    mid = _quote_for_iv(spot, strike, 0.40, t_years)
    bm = _binary(strike, mid)
    base = evaluate_book(spot=spot, sigma_rv=1.0, binaries=[bm],
                         threshold=0.05, hedge_ratio=1.0).signals[0]
    half = evaluate_book(spot=spot, sigma_rv=1.0, binaries=[bm],
                         threshold=0.05, hedge_ratio=0.5).signals[0]
    assert abs(half.perp_hedge_units - 0.5 * base.perp_hedge_units) < 1e-9


def test_aggregates_book_totals():
    spot, t_years = 80_000, 1 / 365
    binaries = []
    for i in range(-3, 4):
        if i == 0:
            continue   # skip ATM (degenerate)
        strike = 80_000 + i * 2_000
        mid = _quote_for_iv(spot, strike, 0.40, t_years)
        binaries.append(_binary(strike, mid))
    res = evaluate_book(spot=spot, sigma_rv=1.0, binaries=binaries,
                        threshold=0.02, hedge_ratio=1.0,
                        notional_per_leg=10_000.0)
    assert res.n_active >= 1
    assert res.gross_notional_usd == res.n_active * 10_000.0
    sum_legs = sum(s.perp_hedge_units for s in res.signals if s.direction != 0)
    assert abs(res.perp_hedge_btc - sum_legs) < 1e-9
