# HIP-4 Animated Alpha Surface

Statistical-arbitrage dashboard between Hyperliquid HIP-4 outcome markets
and BTC perp. The surface animates in real time and re-renders on every
slider change, so generated alpha is visible the moment a variable moves.

![animated alpha surface](docs/surface.gif)

Each frame is a real Hyperliquid snapshot — BTC perp mid, every HIP-4
outcome order book, and the resulting `σ_imp − σ_rv` colouring of the
surface. The header band tracks the running BTC perp delta hedge and
the day-rate alpha being generated.

## The thesis

HIP-4 outcome contracts pay $1 if `BTC > K` at expiry, else 0. Their
mid-price is therefore the risk-neutral probability `P(S_T > K)`. Inverting
that probability under GBM gives an **implied volatility** `σ_imp`. The
BTC perpetual tape gives a **realised volatility** `σ_rv`. The trade:

```
   if σ_imp < σ_rv − threshold     →   LONG  outcome,  SHORT  BTC perp delta
   if σ_imp > σ_rv + threshold     →   SHORT outcome,  LONG   BTC perp delta
```

The book is delta-neutralised every tick using the digital delta
`Δ = n(d₂)/(S σ √T)`. Expected daily P&L per position is the standard
gamma-carry formula

```
   E[P&L/day] = ½ · Γ · S² · (σ_rv² − σ_imp²) / 365
```

## The animated surface

3D plot, refreshed on every tick:

| Axis | Meaning |
|---|---|
| X | moneyness `K / S` |
| Y | hours to expiry |
| Z | implied volatility |
| color | `σ_imp − σ_rv` mapped diverging green ↔ red |
| reference plane | `σ_rv` (white, transparent) |

Sliders re-render the surface instantly:

- **RV window** (5–180 min) — period of the BTC perp tape used for `σ_rv`
- **Threshold** (0.5–30 vol pts) — minimum gap before a position is taken
- **Hedge ratio** (0×–2×) — multiplier on the digital delta when sizing
  the perp hedge

A ▶ Play button animates through the last 60 ticks; the time slider scrubs
through history. Below the surface a P&L panel shows cumulative
theoretical alpha, $/day, and active legs over the session.

## Run

```bash
pip install -r requirements.txt
```

### Interactive dashboard

```bash
# Live — talks to api.hyperliquid.xyz; user state requires the env var.
export HYPERLIQUID_USER_ADDRESS=0xYourAddress
export HYPERLIQUID_API_WALLET_KEY=0xApiSubaccountKey   # for the (gated) execution path
python -m src.app

# Replay a CSV tape captured by you locally:
python -m src.app --csv data/

# Pure offline demo (drifting synthetic universe):
python -m src.app --no-live
```

Then open http://127.0.0.1:8050. Sliders re-render the surface instantly:

- **RV window** (5–180 min) — perp-tape period for `σ_rv`
- **Threshold** (0.5–30 vol pts) — minimum |σ_imp − σ_rv| to take a leg
- **Hedge ratio** (0×–2×) — multiplier on the digital Δ when sizing the perp hedge

### Render the animated GIF

`docs/surface.gif` shown at the top is produced by `scripts/render_animated_gif.py`.
The same renderer runs against any of the three data sources:

```bash
# Real Hyperliquid feed → animated surface that reflects every
# BTC perp tick + outcome book change live:
python scripts/render_animated_gif.py --source live --frames 60 --fps 6

# Replay a captured CSV tape:
python scripts/render_animated_gif.py --source csv --csv-dir data/

# Synthetic drift (no network — useful for demo / CI):
python scripts/render_animated_gif.py --source sim
```

Each frame is a fully-recomputed snapshot — current BTC mid, the hedged
perp position, the IV(K,T) surface from the live order books, and the
alpha-PnL strip — so the GIF is exactly the dashboard's view scrolled
through wall-clock time.

### Local data capture (only used to seed `--csv`)

`scripts/fetch_hl.py` is stdlib-only; run it where the network can reach
Hyperliquid and feed the resulting `data/` directory to the dashboard or
the renderer:

```bash
python scripts/fetch_hl.py --out-dir data/ --interval 5 --duration 600
```

The dashboard itself talks straight to `api.hyperliquid.xyz` once
`HYPERLIQUID_USER_ADDRESS` is exported — `fetch_hl.py` is only needed
for offline replays.

## Layout

```
src/
  hl_client.py     /info client; reads outcomeMeta, l2Book, allMids
  contracts.py     BinaryMarket / TernaryMarket
  pricing.py       prob_above, iv_from_prob, digital Δ/Γ/Vega, carry
  statarb.py       IV-vs-RV signal → direction, hedge, expected $/day
  feed.py          orchestrates client / CSV / simulator + history
  simulator.py     drifting synthetic universe (offline mode)
  data_loader.py   CSV replay
  surface.py       animated 3D surface + cumulative-alpha panel
  app.py           Dash entrypoint with sliders
scripts/
  fetch_hl.py             stdlib-only data capture (you run, dashboard replays)
  render_animated_gif.py  composite GIF renderer (live | csv | sim)
  render_screenshots.py   single-frame static export
  smoke.py                end-to-end check
```
