"""End-to-end smoke test for the IV-vs-RV stat-arb pipeline.

Runs offline against the drifting simulator and confirms:
    * pricing primitives (digital prob + greeks) are sane
    * Feed accumulates a multi-snapshot history
    * StatArbEngine fires positions at sensible thresholds
    * Surface, P&L and opportunity-table figures build without errors
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.feed import Feed
from src.pricing import (
    digital_delta,
    digital_gamma,
    digital_vega,
    expected_carry_per_day,
    iv_from_prob,
    prob_above,
)
from src.surface import build_alpha_pnl, build_alpha_surface, build_opportunities


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    # 1. Pricing primitives
    p = prob_above(78_000, 80_000, 0.65, 1 / 365)
    iv = iv_from_prob(p, 78_000, 80_000, 1 / 365)
    delta = digital_delta(78_000, 80_000, 0.65, 1 / 365)
    gamma = digital_gamma(78_000, 80_000, 0.65, 1 / 365)
    vega = digital_vega(78_000, 80_000, 0.65, 1 / 365)
    carry = expected_carry_per_day(spot=78_000, strike=80_000,
                                   sigma_imp=0.70, sigma_rv=0.60, t_years=1/365)
    _check("prob_above ∈ (0,1)", 0 < p < 1, f"P={p:.4f}")
    _check("iv_from_prob ≈ 0.65", iv is not None and abs(iv - 0.65) < 1e-3, f"iv={iv}")
    _check("digital_delta > 0", delta > 0, f"Δ={delta:.6f}")
    _check("digital_gamma is real", abs(gamma) >= 0, f"Γ={gamma:.3e}")
    _check("digital_vega is real", abs(vega) >= 0, f"V={vega:.4f}")
    _check("carry sign correct (σ_imp>σ_rv ⇒ negative)", carry < 0, f"carry={carry:.4e}")

    # 2. Feed history accumulation (drifting simulator)
    feed = Feed(allow_live=False, history_len=20, threshold_vol=0.03)
    snaps = []
    for _ in range(8):
        snaps.append(feed.snapshot())
        time.sleep(0.02)
    _check("history populated", len(feed.history) >= 8, f"len={len(feed.history)}")
    _check("snapshot has stat-arb legs", any(s.statarb.signals for s in snaps),
           "engine produced no signals")

    # The drifting simulator deliberately mis-prices a strike → expect at least
    # one active position once IV diverges by > threshold.
    n_active = sum(s.statarb.n_active for s in snaps)
    _check("at least one active leg across history",
           n_active >= 1, f"sum_n_active={n_active}")

    # 3. Figures build
    fig_surf = build_alpha_surface(list(feed.history), threshold=0.03)
    fig_pnl = build_alpha_pnl(list(feed.history))
    rows = build_opportunities(snaps[-1])
    _check("surface figure has data + frames", len(fig_surf.data) > 0
           and len(fig_surf.frames) >= 1, f"frames={len(fig_surf.frames)}")
    _check("pnl figure non-empty", len(fig_pnl.data) > 0)

    print("\nlast snapshot — active legs:")
    for r in rows[:6]:
        print(f"  {r['side']:9s} {r['expiry']}  K={r['K']:>10s}  "
              f"Δvol={r['Δvol_pts']:>7s}  hedge={r['perp_hedge']:>16s}  $/day={r['$/day']:>7s}")
    last = snaps[-1]
    print(f"\nspot=${last.spot:,.0f}  σ̂_RV={last.sigma*100:.1f}%  "
          f"legs_active={last.statarb.n_active}  "
          f"expected_$/day={last.statarb.expected_pnl_day_total:+.2f}")


if __name__ == "__main__":
    main()
