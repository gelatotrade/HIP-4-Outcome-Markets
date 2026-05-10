"""Render the animated alpha surface as a GIF.

Per frame:
  * pull the next MarketSnapshot from the configured source (live API,
    CSV replay, or drifting simulator)
  * render the 3D IV(K,T) surface with the σ_RV reference plane
  * render the cumulative-α PnL strip for the rolling history
  * composite both panels + a stats banner with PIL
  * append the frame to a GIF via imageio

Each frame thereby reflects (a) the current BTC perp mid, (b) the
running delta-hedge in BTC perp units, and (c) the live outcome-market
order books — exactly the variables you asked to visualise.

Usage:
    python scripts/render_animated_gif.py                       # simulator
    python scripts/render_animated_gif.py --source live         # api.hyperliquid.xyz
    python scripts/render_animated_gif.py --source csv \\
        --csv-dir data/                                         # captured tape

Common flags:
    --frames N           number of frames (default 36)
    --fps N              playback speed (default 6)
    --tick-seconds S     simulated time advance per frame (sim only, default 60)
    --out PATH           output gif (default docs/surface.gif)
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import imageio.v2 as imageio
import plotly.graph_objects as go
import plotly.io as pio
from PIL import Image, ImageDraw, ImageFont

from src.feed import Feed, MarketSnapshot
from src.simulator import fast_forward
from src.surface import _grid_from_snapshot, _surface_traces, build_alpha_pnl


FRAME_W = 1400
FRAME_H = 920
SURFACE_H = 620
PNL_H = 240
BANNER_H = 60


def _surface_figure(snap: MarketSnapshot, threshold: float) -> go.Figure:
    moneyness, hours, iv, edge = _grid_from_snapshot(snap)
    fig = go.Figure(data=_surface_traces(moneyness, hours, iv, edge, snap.sigma, threshold))
    fig.update_layout(
        width=FRAME_W, height=SURFACE_H,
        scene=dict(
            xaxis=dict(title="K / S (moneyness)", color="#e6edf3",
                       gridcolor="#30363d", backgroundcolor="#0d1117"),
            yaxis=dict(title="Hours to expiry", color="#e6edf3",
                       gridcolor="#30363d", backgroundcolor="#0d1117"),
            zaxis=dict(title="Implied vol", color="#e6edf3",
                       gridcolor="#30363d", backgroundcolor="#0d1117",
                       range=[0.45, 1.30]),
            aspectmode="cube", bgcolor="#0d1117",
            camera=dict(eye=dict(x=1.7, y=-1.9, z=0.85)),
        ),
        paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
        margin=dict(l=0, r=0, t=0, b=0),
    )
    return fig


def _render_png(fig: go.Figure) -> Image.Image:
    raw = pio.to_image(fig, format="png")
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Menlo.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _banner(snap: MarketSnapshot, frame_idx: int, total_frames: int) -> Image.Image:
    img = Image.new("RGBA", (FRAME_W, BANNER_H), color=(13, 17, 23, 255))
    draw = ImageDraw.Draw(img)
    bold = _font(20)
    light = _font(14)

    sa = snap.statarb
    hedge_usd = sa.perp_hedge_btc * snap.spot

    draw.text((20, 12), "HIP-4 alpha surface — IV(K,T) vs σ_RV (BTC perp)",
              fill=(230, 237, 243, 255), font=bold)

    stats = (
        f"BTC ${snap.spot:>10,.0f}   "
        f"σ_RV {snap.sigma*100:>4.1f}%   "
        f"book ${sa.gross_notional_usd:>9,.0f}   "
        f"perp hedge {sa.perp_hedge_btc:+.4f} BTC (${hedge_usd:+,.0f})   "
        f"legs {sa.n_active:>2d}   "
        f"α ${sa.expected_pnl_day_total:+8.2f}/day   "
        f"src {snap.source}   "
        f"frame {frame_idx + 1:>3d}/{total_frames:<3d}"
    )
    draw.text((20, 38), stats, fill=(180, 200, 220, 255), font=light)
    return img


def _composite_frame(snap: MarketSnapshot, history: list[MarketSnapshot],
                     threshold: float, frame_idx: int, total_frames: int) -> Image.Image:
    surf_img = _render_png(_surface_figure(snap, threshold))
    pnl_fig = build_alpha_pnl(history)
    pnl_fig.update_layout(width=FRAME_W, height=PNL_H, title=None,
                          margin=dict(l=10, r=10, t=8, b=30))
    pnl_img = _render_png(pnl_fig)

    canvas = Image.new("RGBA", (FRAME_W, FRAME_H), color=(13, 17, 23, 255))
    canvas.paste(_banner(snap, frame_idx, total_frames), (0, 0))
    canvas.paste(surf_img.resize((FRAME_W, SURFACE_H)), (0, BANNER_H))
    canvas.paste(pnl_img.resize((FRAME_W, PNL_H)), (0, BANNER_H + SURFACE_H))
    return canvas.convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["sim", "live", "csv"], default="sim")
    ap.add_argument("--csv-dir", default=None)
    ap.add_argument("--frames", type=int, default=36)
    ap.add_argument("--fps", type=int, default=6)
    ap.add_argument("--tick-seconds", type=float, default=60.0,
                    help="Simulator time advance per frame (sim only)")
    ap.add_argument("--threshold", type=float, default=0.04)
    ap.add_argument("--notional", type=float, default=10_000.0,
                    help="Demo book size per active leg (USD)")
    ap.add_argument("--out", default="docs/surface.gif")
    args = ap.parse_args()

    common = dict(history_len=120, threshold_vol=args.threshold,
                  notional_per_leg=args.notional)
    if args.source == "live":
        feed = Feed(allow_live=True, **common)
    elif args.source == "csv":
        if not args.csv_dir:
            ap.error("--csv-dir required when --source csv")
        feed = Feed(allow_live=False, csv_path=args.csv_dir, **common)
    else:
        feed = Feed(allow_live=False, **common)

    # Warm-up: gather enough history for σ_RV + a non-trivial PnL trace.
    print(f"[render] source={args.source} warming up…", flush=True)
    for _ in range(15):
        if args.source == "sim":
            fast_forward(args.tick_seconds)
        feed.snapshot()

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frames: list[Image.Image] = []
    t0 = time.time()
    for idx in range(args.frames):
        if args.source == "sim":
            fast_forward(args.tick_seconds)
        snap = feed.snapshot()
        frames.append(_composite_frame(snap, list(feed.history),
                                        args.threshold, idx, args.frames))
        elapsed = time.time() - t0
        print(f"[render] frame {idx+1}/{args.frames}  "
              f"BTC=${snap.spot:,.0f}  legs={snap.statarb.n_active}  "
              f"α=${snap.statarb.expected_pnl_day_total:+.2f}/day  "
              f"({elapsed:.1f}s)", flush=True)

    # Imageio's GIF writer expects ndarray frames, but PIL works directly.
    duration_ms = int(1000 / max(args.fps, 1))
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=False,
    )
    print(f"[render] wrote {out_path} — {args.frames} frames @ {args.fps} fps")


if __name__ == "__main__":
    main()
