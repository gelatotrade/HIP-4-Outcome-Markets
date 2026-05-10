"""Replay HIP-4 captures (CSV from `scripts/fetch_hl.py`) into snapshots.

Used by the dashboard when `--csv data/` is passed. Each call to
`next_snapshot()` advances one tick; `len()` reports total ticks.
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from dataclasses import dataclass, field

from .hl_client import L2Book, L2Level, OutcomeAsset


@dataclass
class _Tick:
    ts: float
    perp_mid: float | None
    rows: list[dict] = field(default_factory=list)


class CSVReplay:
    """Loads `outcomes.csv` + `perp.csv` and exposes one snapshot per tick."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._ticks: list[_Tick] = []
        self._idx = 0
        self._load()

    # ---- I/O -----------------------------------------------------------

    def _load(self) -> None:
        outcomes = os.path.join(self.path, "outcomes.csv")
        perp = os.path.join(self.path, "perp.csv")
        if not os.path.exists(outcomes):
            raise FileNotFoundError(outcomes)

        perp_by_ts: dict[float, float | None] = {}
        if os.path.exists(perp):
            with open(perp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        ts = round(float(row["ts"]), 2)
                        mid = float(row["mid"]) if row.get("mid") else None
                        perp_by_ts[ts] = mid
                    except (KeyError, ValueError):
                        continue

        rows_by_ts: dict[float, list[dict]] = defaultdict(list)
        with open(outcomes, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts = round(float(row["ts"]), 2)
                except (KeyError, ValueError):
                    continue
                rows_by_ts[ts].append(row)

        for ts in sorted(rows_by_ts.keys()):
            self._ticks.append(_Tick(ts=ts, perp_mid=perp_by_ts.get(ts), rows=rows_by_ts[ts]))

    def __len__(self) -> int:
        return len(self._ticks)

    # ---- Snapshot building --------------------------------------------

    def reset(self) -> None:
        self._idx = 0

    def at(self, idx: int) -> tuple[list[OutcomeAsset], dict[str, L2Book], float | None]:
        idx = max(0, min(idx, len(self._ticks) - 1))
        return self._materialise(self._ticks[idx])

    def next_tick(self) -> tuple[list[OutcomeAsset], dict[str, L2Book], float | None]:
        if not self._ticks:
            return [], {}, None
        result = self._materialise(self._ticks[self._idx])
        self._idx = (self._idx + 1) % len(self._ticks)
        return result

    @staticmethod
    def _materialise(tick: _Tick) -> tuple[list[OutcomeAsset], dict[str, L2Book], float | None]:
        assets: list[OutcomeAsset] = []
        books: dict[str, L2Book] = {}
        for r in tick.rows:
            try:
                target = float(r["target"]) if r.get("target") else None
                bid = float(r["bid"]) if r.get("bid") else None
                ask = float(r["ask"]) if r.get("ask") else None
                bid_sz = float(r["bid_sz"]) if r.get("bid_sz") else 0.0
                ask_sz = float(r["ask_sz"]) if r.get("ask_sz") else 0.0
            except ValueError:
                continue
            if target is None:
                continue
            parsed = {
                "class": r.get("klass", ""),
                "underlying": r.get("underlying", "BTC"),
                "expiry": r.get("expiry", ""),
                "period": r.get("period", ""),
                "targetPrice": target,
            }
            asset = OutcomeAsset(
                asset_id=int(r.get("asset_id") or -1),
                coin=r.get("coin", ""),
                name=r.get("name", ""),
                side=r.get("side", "?"),
                outcome_id=int(r.get("outcome_id") or 0),
                description=f"class:{parsed['class']}|underlying:{parsed['underlying']}|"
                            f"expiry:{parsed['expiry']}|targetPrice:{int(target)}|"
                            f"period:{parsed['period']}",
                parsed=parsed,
            )
            assets.append(asset)
            if bid is not None and ask is not None:
                books[asset.coin] = L2Book(
                    coin=asset.coin,
                    bids=[L2Level(px=bid, sz=bid_sz)],
                    asks=[L2Level(px=ask, sz=ask_sz)],
                    ts_ms=int(tick.ts * 1000),
                )
        return assets, books, tick.perp_mid
