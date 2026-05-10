"""HIP-4 contract abstractions.

A *binary* market pays 1 if BTC settles above `target` (Yes side) else 0.
A *ternary* market is Down / Range / Up around two strikes (K_low, K_high).

We synthesise ternary structures from a strip of binary `priceBinary`
markets that share `expiry` and `underlying` — that is the form HIP-4
already exposes. When real `priceTernary` markets ship, the same
`Ternary` object is constructed straight from `outcomeMeta`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .hl_client import L2Book, OutcomeAsset


def _parse_expiry(expiry: str) -> datetime | None:
    """Parse the HIP-4 expiry format `YYYYMMDD-HHMM` to UTC."""
    if not expiry:
        return None
    try:
        return datetime.strptime(expiry, "%Y%m%d-%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class BinaryMarket:
    """Yes/No on `BTC settles above target` at `expiry`."""

    underlying: str
    target: float
    expiry: datetime
    period: str
    yes: OutcomeAsset
    no: OutcomeAsset
    yes_book: L2Book | None = None
    no_book: L2Book | None = None

    @property
    def t_to_expiry_years(self) -> float:
        secs = (self.expiry - datetime.now(timezone.utc)).total_seconds()
        return max(secs, 0.0) / (365.25 * 24 * 3600)

    @property
    def yes_mid(self) -> float | None:
        return self.yes_book.mid if self.yes_book else None

    @property
    def yes_ask(self) -> float | None:
        return self.yes_book.best_ask if self.yes_book else None

    @property
    def yes_bid(self) -> float | None:
        return self.yes_book.best_bid if self.yes_book else None

    @property
    def no_mid(self) -> float | None:
        return self.no_book.mid if self.no_book else None

    @property
    def no_ask(self) -> float | None:
        return self.no_book.best_ask if self.no_book else None

    @property
    def no_bid(self) -> float | None:
        return self.no_book.best_bid if self.no_book else None


@dataclass
class TernaryMarket:
    """Down / Range / Up over (K_low, K_high), same expiry."""

    underlying: str
    k_low: float
    k_high: float
    expiry: datetime
    period: str
    down: BinaryMarket | None = None       # P(S < k_low)
    range_: BinaryMarket | None = None     # P(k_low <= S <= k_high)
    up: BinaryMarket | None = None         # P(S > k_high)
    synthesised: bool = False              # True when built from binaries
    notes: list[str] = field(default_factory=list)

    @property
    def t_to_expiry_years(self) -> float:
        secs = (self.expiry - datetime.now(timezone.utc)).total_seconds()
        return max(secs, 0.0) / (365.25 * 24 * 3600)


def assemble_binary_markets(assets: list[OutcomeAsset]) -> list[BinaryMarket]:
    """Group OutcomeAsset rows into BinaryMarket pairs by outcome_id."""
    by_outcome: dict[int, list[OutcomeAsset]] = {}
    for a in assets:
        by_outcome.setdefault(a.outcome_id, []).append(a)

    markets: list[BinaryMarket] = []
    for sides in by_outcome.values():
        if len(sides) < 2:
            continue
        klass = sides[0].parsed.get("class", "")
        if klass not in ("priceBinary", ""):
            continue
        target_raw = sides[0].parsed.get("targetPrice")
        expiry = _parse_expiry(sides[0].parsed.get("expiry", ""))
        if target_raw is None or expiry is None:
            continue

        yes = next((s for s in sides if s.side.upper().startswith("Y")), None)
        no = next((s for s in sides if s.side.upper().startswith("N")), None)
        if yes is None or no is None:
            yes, no = sides[0], sides[1]

        markets.append(
            BinaryMarket(
                underlying=str(sides[0].parsed.get("underlying", "BTC")),
                target=float(target_raw),
                expiry=expiry,
                period=str(sides[0].parsed.get("period", "")),
                yes=yes,
                no=no,
            )
        )
    markets.sort(key=lambda m: (m.underlying, m.expiry, m.target))
    return markets


def synthesise_ternaries(
    binaries: list[BinaryMarket],
    *,
    band_pct: float = 0.02,
) -> list[TernaryMarket]:
    """For every (underlying, expiry) bucket with >= 2 strikes, build a
    Down/Range/Up triple where K_low and K_high straddle the median strike
    by `band_pct` (default ±2%).

    The middle binary serves a dual role: its NO side approximates the
    Down-or-Range payoff and its YES side approximates Range-or-Up; we
    derive the IN-RANGE leg from the difference of two binaries.
    """
    by_key: dict[tuple[str, datetime], list[BinaryMarket]] = {}
    for b in binaries:
        by_key.setdefault((b.underlying, b.expiry), []).append(b)

    ternaries: list[TernaryMarket] = []
    for (underlying, expiry), strip in by_key.items():
        strip.sort(key=lambda m: m.target)
        if len(strip) < 2:
            continue
        # Pick the pair closest to a band around the centre strike.
        centre = strip[len(strip) // 2].target
        k_low = max((m.target for m in strip if m.target <= centre * (1 - band_pct)), default=None)
        k_high = min((m.target for m in strip if m.target >= centre * (1 + band_pct)), default=None)
        if k_low is None or k_high is None or k_low >= k_high:
            # Fallback: use the two strikes flanking the centre.
            below = [m for m in strip if m.target < centre]
            above = [m for m in strip if m.target > centre]
            if not below or not above:
                continue
            k_low = below[-1].target
            k_high = above[0].target
        if k_low >= k_high:
            continue
        m_low = next(m for m in strip if m.target == k_low)
        m_high = next(m for m in strip if m.target == k_high)

        # Synthesise an IN-RANGE pseudo-binary using the two strip members:
        # Range payoff = Yes(K_low) - Yes(K_high)
        ternaries.append(
            TernaryMarket(
                underlying=underlying,
                k_low=k_low,
                k_high=k_high,
                expiry=expiry,
                period=strip[0].period,
                down=m_low,        # No(K_low)  ≈ P(S < K_low)
                range_=m_high,     # placeholder; pricing module reads both legs
                up=m_high,         # Yes(K_high) ≈ P(S > K_high)
                synthesised=True,
                notes=[
                    "Down  = NO side of the K_low binary",
                    "Up    = YES side of the K_high binary",
                    "Range = YES(K_low) - YES(K_high)",
                ],
            )
        )
    ternaries.sort(key=lambda t: (t.underlying, t.expiry, t.k_low))
    return ternaries
