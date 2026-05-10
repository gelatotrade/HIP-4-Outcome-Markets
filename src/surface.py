"""Plotly figure builders for the animated alpha surface.

Two charts:
  1. `build_alpha_surface(history)` — 3D surface. X = moneyness K/S,
     Y = TTE (hours), Z = market IV. A second plane at Z = realised vol
     (RV) is rendered for reference. Colour = (IV − RV) × hedge × ...,
     mapped onto a diverging green/red scale so the alpha lights up.
     Plotly frames animate every snapshot in `history`.

  2. `build_alpha_pnl(history)` — cumulative theoretical P&L of the
     stat-arb book over the history window so the user can see alpha
     accreting as variables change.

Colour code:
    green  = long-vol edge   (IV < RV — buy outcome, short BTC delta)
    red    = short-vol edge  (IV > RV — sell outcome, long BTC delta)
    grey   = below threshold
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import plotly.graph_objects as go

from .feed import MarketSnapshot

COLOR_LONG_VOL = "#1ec27a"
COLOR_SHORT_VOL = "#e64545"
COLOR_NEUTRAL = "rgba(120,120,140,0.55)"
COLOR_RV_PLANE = "rgba(255,255,255,0.18)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hours_to_expiry(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 3600)


def _grid_from_snapshot(snap: MarketSnapshot) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (moneyness, hours, IV grid, IV-RV grid) for one snapshot."""
    expiries = sorted({s.expiry_iso for s in snap.statarb.signals})
    targets = sorted({s.target for s in snap.statarb.signals})
    if not expiries or not targets:
        return np.array([]), np.array([]), np.zeros((0, 0)), np.zeros((0, 0))

    exp_idx = {e: i for i, e in enumerate(expiries)}
    tgt_idx = {t: i for i, t in enumerate(targets)}
    iv_grid = np.full((len(expiries), len(targets)), np.nan)
    edge_grid = np.full_like(iv_grid, np.nan)
    for s in snap.statarb.signals:
        i, j = exp_idx[s.expiry_iso], tgt_idx[s.target]
        if s.iv is not None:
            iv_grid[i, j] = s.iv
            edge_grid[i, j] = (s.iv - s.rv) if s.iv is not None else np.nan

    moneyness = np.array(targets) / max(snap.spot, 1e-9)
    hours = np.array([_hours_to_expiry(e) for e in expiries])
    return moneyness, hours, iv_grid, edge_grid


def _surface_traces(
    moneyness: np.ndarray, hours: np.ndarray,
    iv: np.ndarray, edge: np.ndarray, rv: float,
    threshold: float,
) -> list[go.Surface]:
    if iv.size == 0:
        return []
    surface_color = np.where(np.abs(edge) >= threshold, edge, np.nan)
    iv_surface = go.Surface(
        x=moneyness, y=hours, z=iv,
        surfacecolor=surface_color,
        colorscale=[
            [0.00, COLOR_LONG_VOL],
            [0.50, "rgba(120,120,140,0.6)"],
            [1.00, COLOR_SHORT_VOL],
        ],
        cmid=0.0, cmin=-0.30, cmax=+0.30,
        colorbar=dict(title="IV − RV", thickness=12),
        hovertemplate=(
            "K/S=%{x:.3f}<br>TTE=%{y:.1f} h<br>"
            "IV=%{z:.3f}<br>edge=%{surfacecolor:+.3f}<extra></extra>"
        ),
        opacity=0.92, name="IV(K,T)",
        lighting=dict(ambient=0.55, diffuse=0.7), showscale=True,
    )
    rv_plane = go.Surface(
        x=moneyness, y=hours, z=np.full_like(iv, rv),
        showscale=False, opacity=0.30,
        colorscale=[[0.0, COLOR_RV_PLANE], [1.0, COLOR_RV_PLANE]],
        hovertemplate=f"RV plane = {rv:.3f}<extra></extra>",
        name="RV",
    )
    return [iv_surface, rv_plane]


# ---------------------------------------------------------------------------
# 1. Animated alpha surface
# ---------------------------------------------------------------------------


def build_alpha_surface(history: list[MarketSnapshot], *, threshold: float = 0.05) -> go.Figure:
    if not history:
        return go.Figure(layout=go.Layout(title="(no snapshots yet)"))

    snap = history[-1]
    moneyness, hours, iv_grid, edge_grid = _grid_from_snapshot(snap)
    base = _surface_traces(moneyness, hours, iv_grid, edge_grid, snap.sigma, threshold)

    frames = []
    for k, h_snap in enumerate(history[-60:]):       # cap animation to 60 frames
        m, hr, iv, eg = _grid_from_snapshot(h_snap)
        if iv.size == 0:
            continue
        frames.append(go.Frame(
            name=str(k),
            data=_surface_traces(m, hr, iv, eg, h_snap.sigma, threshold),
        ))

    fig = go.Figure(data=base, frames=frames)
    fig.update_layout(
        title=(
            f"Animated alpha surface — IV(K,T) over RV plane · "
            f"BTC ${snap.spot:,.0f} · σ̂_RV={snap.sigma*100:.1f}% · "
            f"threshold={threshold*100:.0f} vol pts"
        ),
        scene=dict(
            xaxis=dict(title="K / S (moneyness)", color="#e6edf3"),
            yaxis=dict(title="Hours to expiry", color="#e6edf3"),
            zaxis=dict(title="Implied vol", color="#e6edf3", range=[0.2, 1.5]),
            aspectmode="cube", bgcolor="#0d1117",
            camera=dict(eye=dict(x=1.5, y=-1.7, z=0.9)),
        ),
        paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
        margin=dict(l=0, r=0, t=44, b=0), height=620,
        updatemenus=[dict(
            type="buttons", direction="left",
            x=0.02, y=1.05, xanchor="left", yanchor="top",
            buttons=[
                dict(label="Play", method="animate",
                     args=[None, dict(frame=dict(duration=120, redraw=True),
                                      fromcurrent=True, transition=dict(duration=80))]),
                dict(label="Pause", method="animate",
                     args=[[None], dict(frame=dict(duration=0, redraw=False),
                                        mode="immediate")]),
            ],
            bgcolor="#21262d", bordercolor="#30363d", font=dict(color="#e6edf3"),
        )],
        sliders=[dict(
            active=max(0, len(frames) - 1), pad=dict(t=40),
            x=0.10, len=0.85, currentvalue=dict(prefix="frame "),
            steps=[
                dict(method="animate", label=str(k),
                     args=[[str(k)], dict(mode="immediate",
                                          frame=dict(duration=0, redraw=True),
                                          transition=dict(duration=0))])
                for k in range(len(frames))
            ],
            bgcolor="#21262d", activebgcolor="#1ec27a", font=dict(color="#e6edf3"),
        )] if frames else [],
    )
    return fig


# ---------------------------------------------------------------------------
# 2. Cumulative alpha generated by the stat-arb book
# ---------------------------------------------------------------------------


def build_alpha_pnl(history: list[MarketSnapshot]) -> go.Figure:
    fig = go.Figure()
    if not history:
        return fig

    ts0 = history[0].ts
    xs = [(h.ts - ts0) for h in history]
    expected_per_day = [h.statarb.expected_pnl_day_total for h in history]
    n_active = [h.statarb.n_active for h in history]
    iv_minus_rv_avg = []
    for h in history:
        ivs = [s.iv for s in h.statarb.signals if s.iv is not None]
        if ivs:
            iv_minus_rv_avg.append(np.mean(ivs) - h.sigma)
        else:
            iv_minus_rv_avg.append(0.0)

    # Integrate expected daily P&L ⇒ realised theoretical alpha over the
    # observation window (in seconds).
    cumulative = []
    acc = 0.0
    for k, h in enumerate(history):
        if k == 0:
            cumulative.append(0.0); continue
        dt = max(history[k].ts - history[k-1].ts, 0.0)
        acc += history[k-1].statarb.expected_pnl_day_total * dt / 86_400.0
        cumulative.append(acc)

    fig.add_trace(go.Scatter(
        x=xs, y=cumulative, mode="lines", name="cumulative alpha (USD)",
        line=dict(color=COLOR_LONG_VOL, width=2),
        hovertemplate="t=%{x:.0f}s<br>α=$%{y:.3f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=expected_per_day, mode="lines", name="expected $/day",
        line=dict(color="#7aa7ff", width=1, dash="dot"), yaxis="y2",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=n_active, mode="lines", name="active legs",
        line=dict(color="#ffc939", width=1, dash="dash"), yaxis="y3",
    ))

    fig.update_layout(
        title="Generated alpha — cumulative theoretical P&L of the IV-vs-RV book",
        xaxis=dict(title="seconds since session start", color="#e6edf3", gridcolor="#30363d"),
        yaxis=dict(title="cumulative α (USD)", color=COLOR_LONG_VOL, gridcolor="#30363d"),
        yaxis2=dict(title="$/day", overlaying="y", side="right", color="#7aa7ff", showgrid=False),
        yaxis3=dict(overlaying="y", side="right", anchor="free", position=1.0,
                    color="#ffc939", showgrid=False, title="legs"),
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#e6edf3"), height=320, margin=dict(l=10, r=10, t=40, b=30),
        legend=dict(orientation="h", x=0, y=1.10),
    )
    return fig


# ---------------------------------------------------------------------------
# 3. Stat-arb opportunity table (small, focused)
# ---------------------------------------------------------------------------


def build_opportunities(snap: MarketSnapshot) -> list[dict]:
    rows = []
    for s in sorted(snap.statarb.signals, key=lambda x: -abs(x.edge_vol or 0)):
        if s.direction == 0:
            continue
        rows.append({
            "expiry": s.expiry_iso[:16],
            "K": f"{s.target:,.0f}",
            "IV": f"{s.iv*100:.1f}%" if s.iv is not None else "—",
            "RV": f"{s.rv*100:.1f}%",
            "Δvol_pts": f"{(s.edge_vol or 0)*100:+.1f}",
            "side": "LONG-YES" if s.direction == +1 else "SHORT-YES",
            "perp_hedge": f"{s.perp_hedge_units:+.4f} BTC",
            "$/day": f"{s.expected_pnl_day:+.3f}",
        })
    return rows
