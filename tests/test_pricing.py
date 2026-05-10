"""Unit tests for pricing primitives.

Coverage:
  * prob_above ↔ iv_from_prob round-trip (analytical)
  * digital_delta finite-difference vs analytical
  * digital_gamma sign at ATM and far-OTM
  * digital_vega sign flips around ATM (digital quirk)
  * expected_carry_per_day sign convention
  * butterfly_density flags monotonicity violations
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.pricing import (
    butterfly_density,
    digital_delta,
    digital_gamma,
    digital_vega,
    expected_carry_per_day,
    iv_from_prob,
    prob_above,
)

S = 80_000.0
K = 80_000.0
SIGMA = 0.65
T = 1 / 365


def test_prob_above_in_unit_interval():
    p = prob_above(S, K, SIGMA, T)
    assert 0.0 < p < 1.0


def test_prob_above_above_strike_certain_at_zero_t():
    assert prob_above(K + 1, K, SIGMA, 0.0) == 1.0
    assert prob_above(K - 1, K, SIGMA, 0.0) == 0.0


def test_iv_round_trip_analytical():
    # Round-trip σ → P → σ for non-ATM moneyness (ATM is degenerate)
    for moneyness in (0.92, 0.97, 1.04, 1.10):
        strike = S * moneyness
        p = prob_above(S, strike, SIGMA, T * 7)
        recovered = iv_from_prob(p, S, strike, T * 7)
        assert recovered is not None, f"failed at moneyness={moneyness}"
        assert abs(recovered - SIGMA) < 1e-3


def test_delta_matches_finite_difference():
    h = 0.5
    fd = (prob_above(S + h, K, SIGMA, T) - prob_above(S - h, K, SIGMA, T)) / (2 * h)
    analytic = digital_delta(S, K, SIGMA, T)
    assert math.isclose(fd, analytic, rel_tol=1e-2, abs_tol=1e-7)


def test_gamma_finite_difference():
    h = 50.0
    fd = (
        prob_above(S + h, K, SIGMA, T)
        - 2 * prob_above(S, K, SIGMA, T)
        + prob_above(S - h, K, SIGMA, T)
    ) / (h * h)
    analytic = digital_gamma(S, K, SIGMA, T)
    assert math.isclose(fd, analytic, rel_tol=5e-2, abs_tol=1e-9)


def test_vega_signs():
    # Vega of P(S>K) is *positive* OTM (need vol to push past K) and
    # *negative* ITM (vol works against you when probability is already > 0.5).
    assert digital_vega(S, K * 1.10, SIGMA, T) > 0.0     # OTM
    assert digital_vega(S, K * 0.90, SIGMA, T) < 0.0     # ITM
    # ATM vega is ≈ zero — both d2 wings cancel
    assert abs(digital_vega(S, K, SIGMA, T)) < 0.05


def test_carry_sign_long_vol():
    # If σ_imp < σ_rv, going long the option earns positive expected carry
    # at strikes where γ > 0 (away from ATM degeneracy).
    carry = expected_carry_per_day(spot=S, strike=K * 1.05,
                                   sigma_imp=0.50, sigma_rv=0.70, t_years=T * 7)
    assert carry > 0


def test_carry_sign_short_vol():
    # If σ_imp > σ_rv, gamma carry is negative for a long position
    # (so a SHORT position would be positive).
    carry = expected_carry_per_day(spot=S, strike=K * 1.05,
                                   sigma_imp=0.90, sigma_rv=0.50, t_years=T * 7)
    assert carry < 0


def test_butterfly_density_flags_negative_bin():
    # Strictly monotone P(>K) ⇒ density positive everywhere
    strikes = [70_000, 75_000, 80_000, 85_000, 90_000]
    monotone = [0.95, 0.85, 0.50, 0.20, 0.05]
    dens = butterfly_density(strikes, monotone)
    assert dens.pdf_negative_idx == []

    # Inserted non-monotonicity (P jumps up between two strikes) ⇒ negative bin
    bad = [0.95, 0.50, 0.55, 0.20, 0.05]   # 0.50 -> 0.55 violates
    dens_bad = butterfly_density(strikes, bad)
    assert len(dens_bad.pdf_negative_idx) >= 1


def test_butterfly_density_pdf_integrates_close_to_one():
    # For a wide enough strip, the integral of the bin densities should
    # approximate the total probability mass between K_min and K_max.
    strikes = list(np.linspace(40_000, 160_000, 25))
    probs = [prob_above(S, k, 0.65, 0.25) for k in strikes]
    dens = butterfly_density(strikes, probs)
    integral = float(np.sum(dens.pdf * np.diff(np.asarray(strikes))))
    # Should be close to P(K_min < S < K_max) = P(>K_min) - P(>K_max)
    expected = probs[0] - probs[-1]
    assert math.isclose(integral, expected, abs_tol=1e-3)


def test_iv_inversion_returns_none_for_atm():
    # ATM digitals are degenerate under zero-drift GBM (prob ≤ 0.5 for all σ).
    assert iv_from_prob(0.5, S, S, T) is None


def test_iv_inversion_returns_none_for_unreachable_quote():
    # OTM 1-day digital priced at 0.45 is above the no-arb maximum.
    assert iv_from_prob(0.45, 80_000, 85_000, 1 / 365) is None


def test_iv_inversion_succeeds_otm_realistic():
    # Realistic 1-day 6% OTM quote
    assert iv_from_prob(0.20, 80_000, 85_000, 1 / 365) is not None


@pytest.mark.parametrize("t_years", [-1, 0])
def test_prob_above_zero_or_negative_t(t_years):
    # Degenerate cases: returns 0/1 based on spot vs strike
    assert prob_above(S, K + 1, SIGMA, t_years) in (0.0, 1.0)
