# HIP-4 Outcome Markets — Alpha & Arbitrage Surface

Live alpha / arbitrage dashboard for Hyperliquid HIP-4 outcome contracts
(launched on mainnet 2 May 2026). The surface paints every market on a
moneyness × time-to-expiry grid and colour-codes profitable structures so
mispricings literally light up.

```
green   long edge   (alpha or risk-free buy)
red     short edge
yellow  parity violation  (Yes + No != 1)
pink    butterfly arb     (negative risk-neutral density)
grey    below threshold
```

## What HIP-4 actually trades

| Object | Today | Coming |
|---|---|---|
| `priceBinary` BTC daily / weekly | live | – |
| `priceTernary` (Down / Range / Up around two strikes) | not yet on mainnet — synthesised here from a strip of binaries | will plug in straight from `outcomeMeta` |
| Multi-outcome generic | spec exists, not live | same client surfaces it |

Each binary settles to 1 if `BTC > targetPrice` at `expiry`, else 0.
Yes and No tokens share one merged orderbook on the HyperCore CLOB.
Prices float in `(0.001, 0.999)`.

The contract spec lives in `description`:

```
class:priceBinary|underlying:BTC|expiry:20260503-0600|targetPrice:78213|period:1d
```

API access (`POST https://api.hyperliquid.xyz/info`):

| `type` | purpose |
|---|---|
| `outcomeMeta` | HIP-4 universe |
| `outcomeMetaAndAssetCtxs` | universe + per-asset ctx |
| `l2Book` with `coin: "#N"` | orderbook for one outcome side |
| `allMids` | one-shot mids for everything |
| `meta` | perp universe (used to pull BTC perp anchor) |

## Strategies the surface highlights

1. **Yes/No parity (binary).** `bid_yes + bid_no > 1` => sell both. `ask_yes + ask_no < 1` => buy both. Risk-free, zero open fees.
2. **Strike monotonicity.** `P(BTC>K)` is non-increasing in `K`. Forward-difference density goes negative => butterfly arb between two strikes.
3. **Ternary simplex.** `P(Down) + P(Range) + P(Up) = 1`. Synthesised from two binaries: `P(Range) = P(>K_low) - P(>K_high)`. When native ternaries ship, the *ask* sum below 1 is a direct buy-simplex risk-free.
4. **GBM alpha vs perp anchor.** Invert the binary mid into implied vol; compare to short-window realized vol from the perp price tape. >50 bps gap => alpha leg.
5. **Term-structure consistency.** Same strike across daily / weekly should be vol-coherent. Surface y-axis exposes calendar dislocations.

The first three are *strict* no-arb conditions; the last two are *statistical* edges. Both colour the same surface.

## How the surface is computed

For every binary `(target K, expiry T)`:

- `fair_yes = N(d2)` with `S = BTC perp mid`, `sigma = realized vol`, `T = time to expiry in years`
- `edge_bps = (fair_yes - market_yes_mid) * 10_000`
- `iv = brentq(sigma -> P(S>K, sigma) - market_mid)`

For every (underlying, expiry) strip with `>= 2` strikes, the
risk-neutral density is the forward difference of the digitals:

```
pdf[i] = (P_above[i] - P_above[i+1]) / (K[i+1] - K[i])
```

Negative `pdf[i]` is a hard butterfly arbitrage between strikes.

For each ternary `(K_low, K_high, expiry)` the simplex point is
`(P_down, P_range, P_up)`. The Plotly ternary plot draws every market
point connected to its GBM-fair counterpart so the alpha vector is
immediately visible.

## Run

```bash
pip install -r requirements.txt

# Live mode (calls api.hyperliquid.xyz):
python -m src.app --port 8050

# Offline / demo mode (uses the simulator, planted mispricings):
python -m src.app --port 8050 --no-live
```

Open http://127.0.0.1:8050. The page auto-refreshes every 2 s by default
(`--refresh-ms`). The status bar shows `source=live | partial | simulated`
so you always know what you're looking at; if the API blocks or returns
nothing, the simulator transparently keeps the surface alive.

## Layout

- `src/hl_client.py` — `POST /info` client; defensive parsing of `outcomeMeta`
- `src/contracts.py` — `BinaryMarket` / `TernaryMarket` and the binary→ternary synthesiser
- `src/pricing.py` — GBM digitals, IV inversion, butterfly density, per-leg edge structs
- `src/simulator.py` — synthetic HIP-4 universe with two planted mispricings (smoke + offline demo)
- `src/feed.py` — orchestrates client + pricing into a `MarketSnapshot`
- `src/surface.py` — Plotly figure builders (3D alpha surface, ternary simplex, density bars)
- `src/app.py` — Dash entrypoint
- `scripts/smoke.py` — end-to-end check; run before committing

## Caveats

- Public Hyperliquid REST is rate-limited at ~100 req/min. The dashboard
  fans out one `l2Book` request per outcome side per refresh, so for a
  large universe widen `--refresh-ms` or batch via `outcomeMetaAndAssetCtxs`.
- The realized-vol estimate uses only spot prices captured during the
  current session (rolling 6 h window). Wire in `candleSnapshot` for
  longer history if you want stationary IV-vs-RV signals.
- The synthetic ternary uses the YES leg of the K_high binary as a proxy
  for a native UP token; when Hyperliquid ships `priceTernary`, the
  `synthesise_ternaries` call in `feed.py` is the only thing that needs
  to fork.
