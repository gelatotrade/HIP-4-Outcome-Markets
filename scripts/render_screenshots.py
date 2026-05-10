"""Render a representative animated alpha surface to docs/surface.png.

Uses the drifting simulator to accumulate ~30 ticks of history, then
exports the 3D surface and a P&L trace via plotly+kaleido.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import plotly.io as pio
import plotly.graph_objects as go

from src.feed import Feed
from src.surface import _grid_from_snapshot, _surface_traces, build_alpha_pnl


def _build_static_surface(snap, threshold: float) -> go.Figure:
    moneyness, hours, iv, edge = _grid_from_snapshot(snap)
    base = _surface_traces(moneyness, hours, iv, edge, snap.sigma, threshold)
    fig = go.Figure(data=base)
    fig.update_layout(
        width=1400, height=720,
        title=dict(
            text=(f"<b>HIP-4 alpha surface</b> — IV(K,T) above the σ_RV plane · "
                  f"BTC ${snap.spot:,.0f} · σ_RV={snap.sigma*100:.1f}% · "
                  f"threshold={threshold*100:.0f} vol pts"),
            font=dict(color="#e6edf3", size=14), x=0.02, y=0.97,
        ),
        scene=dict(
            xaxis=dict(title="K / S (moneyness)", color="#e6edf3", gridcolor="#30363d",
                       backgroundcolor="#0d1117"),
            yaxis=dict(title="Hours to expiry", color="#e6edf3", gridcolor="#30363d",
                       backgroundcolor="#0d1117"),
            zaxis=dict(title="Implied vol", color="#e6edf3", gridcolor="#30363d",
                       backgroundcolor="#0d1117", range=[0.45, 1.20]),
            aspectmode="cube", bgcolor="#0d1117",
            camera=dict(eye=dict(x=1.7, y=-1.9, z=0.85)),
        ),
        paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
        margin=dict(l=0, r=0, t=40, b=0),
    )
    return fig


def main() -> None:
    out_dir = pathlib.Path(__file__).resolve().parent.parent / "docs"
    out_dir.mkdir(exist_ok=True)

    feed = Feed(allow_live=False, history_len=60, threshold_vol=0.04)
    for _ in range(45):
        feed.snapshot()
        time.sleep(0.04)

    snap = list(feed.history)[-1]
    surface = _build_static_surface(snap, threshold=0.04)
    pnl = build_alpha_pnl(list(feed.history))
    pnl.update_layout(width=1400, height=320, title=None,
                      margin=dict(l=10, r=10, t=10, b=30))

    pio.write_image(surface, str(out_dir / "surface.png"))
    pio.write_image(pnl, str(out_dir / "pnl.png"))
    print(f"wrote {out_dir/'surface.png'} and {out_dir/'pnl.png'}")


if __name__ == "__main__":
    main()
