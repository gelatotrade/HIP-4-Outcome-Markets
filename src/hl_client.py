"""Thin Hyperliquid Info-endpoint client for HIP-4 outcome markets.

Endpoints used (all `POST https://api.hyperliquid.xyz/info`):
    {"type": "meta"}                       perp universe
    {"type": "spotMeta"}                   spot universe
    {"type": "allMids"}                    every mid in one shot (incl. outcomes)
    {"type": "outcomeMeta"}                HIP-4 outcome universe
    {"type": "outcomeMetaAndAssetCtxs"}    outcome universe + per-asset ctx
    {"type": "l2Book", "coin": "@N"|"#N"}  L2 snapshot for any asset

The shape of `outcomeMeta` is not yet pinned down in public docs; we therefore
treat the response defensively and look for the same keys Hyperliquid uses for
spot / perps (`universe`, `tokens`, `description`, `name`).

Falls back to `simulator.synthetic_universe()` when the network is blocked or
the endpoint returns nothing usable, so the dashboard always boots.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_TIMEOUT = 6.0

log = logging.getLogger(__name__)


@dataclass
class OutcomeAsset:
    """One side of one outcome market (Yes or No, or one of N for multi-outcome)."""

    asset_id: int
    coin: str           # API alias e.g. "#3"
    name: str           # human label e.g. "BTC-78213-Y"
    side: str           # "Y" / "N" / outcome label
    outcome_id: int
    description: str    # raw description string
    parsed: dict[str, Any] = field(default_factory=dict)


@dataclass
class L2Level:
    px: float
    sz: float


@dataclass
class L2Book:
    coin: str
    bids: list[L2Level]
    asks: list[L2Level]
    ts_ms: int

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].px + self.asks[0].px) / 2.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].px if self.asks else None


def _parse_description(desc: str) -> dict[str, Any]:
    """`class:priceBinary|underlying:BTC|expiry:20260503-0600|targetPrice:78213|period:1d`."""
    out: dict[str, Any] = {}
    if not desc:
        return out
    for chunk in desc.split("|"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        out[k.strip()] = v.strip()
    for num_key in ("targetPrice", "lowerStrike", "upperStrike"):
        if num_key in out:
            try:
                out[num_key] = float(out[num_key])
            except ValueError:
                pass
    return out


class HLClient:
    def __init__(self, base_url: str = INFO_URL, timeout: float = DEFAULT_TIMEOUT):
        self._client = httpx.Client(timeout=timeout)
        self._base_url = base_url
        self.last_error: str | None = None

    def _post(self, payload: dict[str, Any]) -> Any:
        try:
            r = self._client.post(self._base_url, json=payload)
            r.raise_for_status()
            self.last_error = None
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("HL request failed (%s): %s", payload.get("type"), exc)
            return None

    # -- Info endpoints ---------------------------------------------------

    def all_mids(self) -> dict[str, float]:
        raw = self._post({"type": "allMids"}) or {}
        return {k: float(v) for k, v in raw.items()}

    def perp_mid(self, coin: str = "BTC") -> float | None:
        mids = self.all_mids()
        return mids.get(coin)

    def outcome_meta(self) -> list[OutcomeAsset]:
        """Best-effort parser; copes with several plausible response shapes."""
        raw = self._post({"type": "outcomeMeta"})
        if raw is None:
            raw = self._post({"type": "outcomeMetaAndAssetCtxs"})
        if raw is None:
            return []

        # Shape A: {"universe": [{...}, ...]}
        # Shape B: [{"universe": [...]}, [ctx, ...]]      (mirrors metaAndAssetCtxs)
        # Shape C: [{...outcome objects...}]
        universe = None
        if isinstance(raw, dict) and "universe" in raw:
            universe = raw["universe"]
        elif isinstance(raw, list) and raw and isinstance(raw[0], dict) and "universe" in raw[0]:
            universe = raw[0]["universe"]
        elif isinstance(raw, list):
            universe = raw

        if not universe:
            return []

        assets: list[OutcomeAsset] = []
        for entry in universe:
            if not isinstance(entry, dict):
                continue
            description = entry.get("description") or entry.get("desc") or ""
            parsed = _parse_description(description)
            sides = entry.get("sides") or entry.get("tokens") or []
            outcome_id = int(entry.get("outcomeId", entry.get("id", len(assets))))

            if sides:
                for s in sides:
                    if not isinstance(s, dict):
                        continue
                    assets.append(
                        OutcomeAsset(
                            asset_id=int(s.get("assetId", s.get("index", -1))),
                            coin=str(s.get("coin", s.get("alias", f"#{s.get('index', -1)}"))),
                            name=str(s.get("name", s.get("label", "?"))),
                            side=str(s.get("side", s.get("label", "?"))),
                            outcome_id=outcome_id,
                            description=description,
                            parsed=parsed,
                        )
                    )
            else:
                # Sometimes the universe entry IS one side already.
                assets.append(
                    OutcomeAsset(
                        asset_id=int(entry.get("assetId", entry.get("index", -1))),
                        coin=str(entry.get("coin", f"#{entry.get('index', -1)}")),
                        name=str(entry.get("name", "?")),
                        side=str(entry.get("side", "?")),
                        outcome_id=outcome_id,
                        description=description,
                        parsed=parsed,
                    )
                )
        return assets

    def l2_book(self, coin: str, depth: int = 5) -> L2Book | None:
        raw = self._post({"type": "l2Book", "coin": coin})
        if not raw:
            return None
        levels = raw.get("levels") if isinstance(raw, dict) else None
        if not levels or len(levels) < 2:
            return None
        bids_raw, asks_raw = levels[0][:depth], levels[1][:depth]

        def _to_levels(rows: list[Any]) -> list[L2Level]:
            out: list[L2Level] = []
            for row in rows:
                try:
                    out.append(L2Level(px=float(row["px"]), sz=float(row["sz"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        return L2Book(
            coin=coin,
            bids=_to_levels(bids_raw),
            asks=_to_levels(asks_raw),
            ts_ms=int(raw.get("time", time.time() * 1000)),
        )

    def close(self) -> None:
        self._client.close()
