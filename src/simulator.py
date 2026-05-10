"""Synthetic HIP-4 universe so the dashboard runs offline.

Spot follows a stateful random walk and one strike carries a slowly mean-
reverting IV anomaly. Each call to `synthetic_universe()` returns the
*next* tick of the simulation so the dashboard's animated surface has
real motion to display.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .hl_client import L2Book, L2Level, OutcomeAsset
from .pricing import prob_above

DEFAULT_SPOT = 78_500.0
DEFAULT_VOL = 0.65
TICK_SECONDS = 1.0


@dataclass
class _SimState:
    spot: float = DEFAULT_SPOT
    realised_vol: float = DEFAULT_VOL
    iv_offset: float = 0.0          # mean-reverting anomaly on the K=1.01 strike
    last_ts: float = field(default_factory=time.time)


_STATE = _SimState()
_RNG = random.Random(0xC0FFEE)


def _step_state() -> _SimState:
    now = time.time()
    dt = max(now - _STATE.last_ts, 1e-6)
    _STATE.last_ts = now
    # Spot GBM step with annualised vol = realised_vol
    sigma_dt = _STATE.realised_vol * math.sqrt(dt / (365.25 * 24 * 3600))
    _STATE.spot *= math.exp(_RNG.gauss(0.0, sigma_dt))
    # Realised vol slowly varies in [40%, 90%]
    _STATE.realised_vol = max(0.40, min(0.90,
        _STATE.realised_vol + _RNG.gauss(0.0, 0.005)))
    # IV anomaly mean-reverts to a regime point that itself oscillates
    target_iv = 0.10 * math.sin(now / 30.0)
    _STATE.iv_offset += 0.15 * (target_iv - _STATE.iv_offset) + _RNG.gauss(0.0, 0.005)
    return _STATE


def _book(mid: float, half_spread: float = 0.005, depth: int = 4) -> L2Book:
    bids = [
        L2Level(px=round(max(0.001, mid - half_spread - i * 0.002), 4),
                sz=round(50 + 30 * i + _RNG.uniform(-10, 10), 1))
        for i in range(depth)
    ]
    asks = [
        L2Level(px=round(min(0.999, mid + half_spread + i * 0.002), 4),
                sz=round(50 + 30 * i + _RNG.uniform(-10, 10), 1))
        for i in range(depth)
    ]
    return L2Book(coin="sim", bids=bids, asks=asks, ts_ms=int(time.time() * 1000))


def synthetic_universe() -> tuple[list[OutcomeAsset], dict[str, L2Book], dict[str, float]]:
    state = _step_state()
    spot = state.spot

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

            # Market-implied vol = realised + structural mispricing on K=1.01
            iv_market = state.realised_vol
            if abs(m - 1.01) < 1e-6:
                iv_market += state.iv_offset                  # the alpha leg
            if tag == "weekly" and abs(m - 1.05) < 1e-6:
                iv_market -= 0.08                              # persistent under-priced wing
            iv_market = max(0.10, min(2.0, iv_market))

            fair_yes = prob_above(spot, target, iv_market, t_years)
            mid_yes = max(0.005, min(0.995, fair_yes))
            mid_no = max(0.005, min(0.995, 1.0 - mid_yes))
            spread = 0.004 if not (tag == "weekly" and abs(m - 1.05) < 1e-6) else 0.001

            yes_coin = f"#{asset_idx}"
            no_coin = f"#{asset_idx + 1}"
            desc = (
                f"class:priceBinary|underlying:BTC|"
                f"expiry:{expiry.strftime('%Y%m%d-%H%M')}|"
                f"targetPrice:{int(target)}|period:{period}"
            )
            parsed = {
                "class": "priceBinary", "underlying": "BTC",
                "expiry": expiry.strftime("%Y%m%d-%H%M"),
                "targetPrice": float(target), "period": period,
            }
            yes = OutcomeAsset(asset_id=asset_idx, coin=yes_coin,
                               name=f"BTC-{int(target)}-{tag}-Y", side="Y",
                               outcome_id=outcome_id, description=desc, parsed=parsed)
            no = OutcomeAsset(asset_id=asset_idx + 1, coin=no_coin,
                              name=f"BTC-{int(target)}-{tag}-N", side="N",
                              outcome_id=outcome_id, description=desc, parsed=parsed)
            assets.extend([yes, no])
            books[yes_coin] = _book(mid_yes, half_spread=spread)
            books[no_coin] = _book(mid_no, half_spread=spread)
            mids[yes_coin] = mid_yes
            mids[no_coin] = mid_no
            asset_idx += 2
            outcome_id += 1
    return assets, books, mids
