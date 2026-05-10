"""Synthetic HIP-4 universe so the dashboard runs offline.

We mock a strip of priceBinary BTC dailies around a moving spot, plus a
couple of weeklies so the term-structure axis isn't degenerate. Two of
the strikes are deliberately mispriced so the arbitrage colours light up
on first launch.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from .hl_client import L2Book, L2Level, OutcomeAsset


def _book(mid: float, half_spread: float = 0.005, depth: int = 4, ts: int | None = None) -> L2Book:
    bids = [
        L2Level(px=round(max(0.001, mid - half_spread - i * 0.002), 4), sz=round(50 + 30 * i, 1))
        for i in range(depth)
    ]
    asks = [
        L2Level(px=round(min(0.999, mid + half_spread + i * 0.002), 4), sz=round(50 + 30 * i, 1))
        for i in range(depth)
    ]
    return L2Book(coin="sim", bids=bids, asks=asks, ts_ms=ts or int(time.time() * 1000))


def synthetic_universe(spot: float = 78_500.0) -> tuple[list[OutcomeAsset], dict[str, L2Book], dict[str, float]]:
    """Build (assets, book_by_coin, all_mids).

    Strikes: spot * {0.95, 0.97, 0.99, 1.00, 1.01, 1.03, 1.05}.
    Two intentional mispricings:
        - K=0.99*spot YES is overpriced by ~3% (short-yes alpha)
        - K=1.05*spot weekly NO is underpriced (parity arb)
    """
    rng = random.Random(0xC0FFEE)
    assets: list[OutcomeAsset] = []
    books: dict[str, L2Book] = {}
    mids: dict[str, float] = {"BTC": spot, "ETH": 3_400.0}

    now = datetime.now(timezone.utc)
    expiries = [
        ("daily", now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1), "1d"),
        ("weekly", now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=7), "7d"),
    ]
    moneyness = [0.95, 0.97, 0.99, 1.00, 1.01, 1.03, 1.05]

    asset_idx = 100_000
    outcome_id = 0
    for tag, expiry, period in expiries:
        for m in moneyness:
            target = round(spot * m, 0)
            t_years = max((expiry - now).total_seconds() / (365.25 * 24 * 3600), 1e-6)
            sigma = 0.65
            from .pricing import prob_above

            fair_yes = prob_above(spot, target, sigma, t_years)
            mid_yes = max(0.005, min(0.995, fair_yes + rng.uniform(-0.01, 0.01)))

            # Intentional anomalies
            if tag == "daily" and abs(m - 0.99) < 1e-6:
                mid_yes = max(0.01, min(0.99, mid_yes + 0.06))   # over-priced YES
            if tag == "weekly" and abs(m - 1.05) < 1e-6:
                mid_yes = max(0.01, min(0.99, mid_yes - 0.05))   # under-priced YES

            mid_no = max(0.005, min(0.995, 1.0 - mid_yes))
            # one parity violation: shrink the spread on weekly 1.05 NO
            spread = 0.004 if not (tag == "weekly" and abs(m - 1.05) < 1e-6) else 0.001

            yes_coin = f"#{asset_idx}"
            no_coin = f"#{asset_idx + 1}"
            desc = (
                f"class:priceBinary|underlying:BTC|"
                f"expiry:{expiry.strftime('%Y%m%d-%H%M')}|"
                f"targetPrice:{int(target)}|period:{period}"
            )
            yes = OutcomeAsset(
                asset_id=asset_idx, coin=yes_coin,
                name=f"BTC-{int(target)}-{tag}-Y", side="Y",
                outcome_id=outcome_id, description=desc,
                parsed={
                    "class": "priceBinary", "underlying": "BTC",
                    "expiry": expiry.strftime("%Y%m%d-%H%M"),
                    "targetPrice": float(target), "period": period,
                },
            )
            no = OutcomeAsset(
                asset_id=asset_idx + 1, coin=no_coin,
                name=f"BTC-{int(target)}-{tag}-N", side="N",
                outcome_id=outcome_id, description=desc,
                parsed=yes.parsed,
            )
            assets.extend([yes, no])
            books[yes_coin] = _book(mid_yes, half_spread=spread)
            books[no_coin] = _book(mid_no, half_spread=spread)
            mids[yes_coin] = mid_yes
            mids[no_coin] = mid_no

            asset_idx += 2
            outcome_id += 1

    return assets, books, mids
