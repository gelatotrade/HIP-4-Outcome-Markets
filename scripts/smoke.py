"""End-to-end smoke test: runs offline against the simulator and verifies
that every layer (parser → pricing → strategies → figure builders) emits
sane output, with at least one arbitrage hit visible on the surface.
"""

from __future__ import annotations

import math
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.feed import Feed
from src.pricing import butterfly_density, prob_above, iv_from_prob
from src.surface import (
    build_alpha_surface,
    build_density,
    build_opportunities,
    build_simplex,
)


def _check(label: str, ok: bool, detail: str = "") -> None:
    flag = "OK " if ok else "FAIL"
    print(f"[{flag}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    # 1. Pricing primitives round-trip
    p = prob_above(78_000, 80_000, 0.65, 1 / 365)
    iv = iv_from_prob(p, 78_000, 80_000, 1 / 365)
    _check("prob_above ∈ (0,1)", 0.0 < p < 1.0, f"P={p:.4f}")
    _check("iv_from_prob ≈ 0.65", iv is not None and abs(iv - 0.65) < 1e-3, f"iv={iv}")

    # 2. Butterfly density flags negative regions
    dens = butterfly_density(
        [70_000, 75_000, 80_000, 85_000, 90_000],
        [0.95, 0.85, 0.50, 0.55, 0.05],   # 0.50 -> 0.55 violates monotonicity
    )
    _check("density flags arb", len(dens.pdf_negative_idx) >= 1, str(dens.pdf_negative_idx))

    # 3. Feed snapshot via simulator
    feed = Feed(allow_live=False)
    snap = feed.snapshot()
    _check("snapshot has binaries", len(snap.binaries) >= 6, f"n={len(snap.binaries)}")
    _check("snapshot has ternaries", len(snap.ternaries) >= 1, f"n={len(snap.ternaries)}")
    _check("at least one alpha hit", any(be.arb_kind for be in snap.binary_edges),
           "no edges fired — simulator should have planted some")
    _check("density per expiry", len(snap.densities) >= 1, f"n={len(snap.densities)}")

    # 4. Figures build without crashing
    f1 = build_alpha_surface(snap)
    f2 = build_simplex(snap)
    f3 = build_density(snap)
    _check("alpha surface trace count > 0", len(f1.data) > 0)
    _check("simplex trace count > 0", len(f2.data) > 0)
    _check("density trace count > 0", len(f3.data) > 0)

    rows = build_opportunities(snap)
    _check("opportunity table non-empty", len(rows) >= 1, f"rows={len(rows)}")

    print("\nopportunities:")
    for r in rows[:5]:
        print(f"  {r['kind']:7s} {r['expiry']}  K={r['strike']}  edge={r['edge_bps']:>6} bps  {r['action']}")
    print(f"\nsource={snap.source}  spot=${snap.spot:,.0f}  σ̂={snap.sigma*100:.1f}%")


if __name__ == "__main__":
    main()
