#!/usr/bin/env python3
"""Standalone Hyperliquid HIP-4 capture → CSV.

Run on a machine with internet access; the resulting CSVs feed the
animated alpha surface (see `src/data_loader.py`).

Output (--out-dir, default ./data):
    outcomes.csv   one row per snapshot per outcome side
                   columns: ts, source, coin, asset_id, name, side,
                            outcome_id, underlying, target, expiry,
                            period, bid, ask, mid, bid_sz, ask_sz
    perp.csv       one row per snapshot
                   columns: ts, coin, mid

Usage:
    python scripts/fetch_hl.py                 # one-shot capture
    python scripts/fetch_hl.py --interval 5    # every 5s, append rows
    python scripts/fetch_hl.py --duration 600  # stop after 10 minutes

No third-party deps — just stdlib.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"

OUTCOME_COLS = [
    "ts", "source", "coin", "asset_id", "name", "side", "outcome_id",
    "underlying", "klass", "target", "expiry", "period",
    "bid", "ask", "mid", "bid_sz", "ask_sz",
]
PERP_COLS = ["ts", "coin", "mid"]


def post(payload: dict, *, timeout: float = 10.0) -> object:
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def parse_description(desc: str) -> dict:
    out: dict = {}
    if not desc:
        return out
    for chunk in desc.split("|"):
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def extract_universe(raw: object) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict) and "universe" in raw:
        return raw["universe"]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "universe" in raw[0]:
        return raw[0]["universe"]
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def fetch_outcomes() -> list[dict]:
    raw = None
    for typ in ("outcomeMeta", "outcomeMetaAndAssetCtxs"):
        try:
            raw = post({"type": typ})
            if raw:
                break
        except urllib.error.URLError as e:
            print(f"[warn] {typ}: {e}", file=sys.stderr)
    universe = extract_universe(raw)
    if not universe:
        print("[warn] outcomeMeta empty — HIP-4 may be testnet-only or disabled", file=sys.stderr)
    sides: list[dict] = []
    for entry in universe:
        if not isinstance(entry, dict):
            continue
        desc = entry.get("description") or entry.get("desc") or ""
        parsed = parse_description(desc)
        outcome_id = entry.get("outcomeId", entry.get("id", -1))
        legs = entry.get("sides") or entry.get("tokens") or []
        if not legs and "coin" in entry:
            legs = [entry]
        for s in legs:
            if not isinstance(s, dict):
                continue
            sides.append({
                "coin": s.get("coin", s.get("alias", f"#{s.get('index', '-1')}")),
                "asset_id": s.get("assetId", s.get("index", -1)),
                "name": s.get("name", s.get("label", "?")),
                "side": s.get("side", s.get("label", "?")),
                "outcome_id": outcome_id,
                "underlying": parsed.get("underlying", ""),
                "klass": parsed.get("class", ""),
                "target": parsed.get("targetPrice", ""),
                "expiry": parsed.get("expiry", ""),
                "period": parsed.get("period", ""),
            })
    return sides


def fetch_book(coin: str) -> tuple[float | None, float | None, float | None, float | None]:
    try:
        raw = post({"type": "l2Book", "coin": coin})
    except urllib.error.URLError as e:
        print(f"[warn] l2Book {coin}: {e}", file=sys.stderr)
        return None, None, None, None
    if not isinstance(raw, dict):
        return None, None, None, None
    levels = raw.get("levels") or []
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return None, None, None, None
    try:
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
        bid_sz = float(levels[0][0]["sz"])
        ask_sz = float(levels[1][0]["sz"])
        return bid, ask, bid_sz, ask_sz
    except (KeyError, TypeError, ValueError):
        return None, None, None, None


def fetch_perp_mid(coin: str = "BTC") -> float | None:
    try:
        raw = post({"type": "allMids"})
    except urllib.error.URLError as e:
        print(f"[warn] allMids: {e}", file=sys.stderr)
        return None
    if isinstance(raw, dict) and coin in raw:
        try:
            return float(raw[coin])
        except (TypeError, ValueError):
            return None
    return None


def write_rows(path: str, header: list[str], rows: list[dict]) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in header})


def capture_once(out_dir: str, source: str = "live") -> None:
    ts = time.time()
    sides = fetch_outcomes()
    rows = []
    for s in sides:
        coin = s["coin"]
        bid, ask, bid_sz, ask_sz = fetch_book(coin)
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        rows.append({
            **s, "ts": ts, "source": source,
            "bid": bid, "ask": ask, "mid": mid,
            "bid_sz": bid_sz, "ask_sz": ask_sz,
        })
    write_rows(os.path.join(out_dir, "outcomes.csv"), OUTCOME_COLS, rows)

    perp = fetch_perp_mid("BTC")
    write_rows(os.path.join(out_dir, "perp.csv"), PERP_COLS, [
        {"ts": ts, "coin": "BTC", "mid": perp},
    ])
    print(f"[{time.strftime('%H:%M:%S')}] captured {len(rows)} outcome rows, "
          f"BTC perp={perp}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="HIP-4 → CSV capture")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="Seconds between captures (0 = one-shot)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="Total seconds to run (0 = forever; ignored if interval=0)")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    if args.interval <= 0:
        capture_once(args.out_dir)
        return
    deadline = time.time() + args.duration if args.duration > 0 else float("inf")
    try:
        while time.time() < deadline:
            capture_once(args.out_dir)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[stop]", file=sys.stderr)


if __name__ == "__main__":
    main()
