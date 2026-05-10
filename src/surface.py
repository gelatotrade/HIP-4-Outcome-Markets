"""Plotly figure builders for the alpha / arbitrage surface.

Three views, one shared colour code:
    green  = profitable BUY edge   (alpha or arb to take long)
    red    = profitable SELL edge
    yellow = strict no-arb violation (parity / butterfly / simplex)
    grey   = below-threshold / no signal
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import plotly.graph_objects as go

from .feed import MarketSnapshot

EDGE_THRESHOLD_BPS = 50.0   # paint anything stronger than 50 bps
PARITY_THRESHOLD = 0.005    # 0.5 cent on a $1 contract = arb
SIMPLEX_THRESHOLD = 0.01

COLOR_BUY = "#1ec27a"
COLOR_SELL = "#e64545"
COLOR_PARITY = "#ffc939"
COLOR_DENSITY_NEG = "#ff7ad9"
COLOR_NEUTRAL = "rgba(120,120,140,0.55)"


def _hours_to_expiry(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 3600)


def _edge_color(edge_bps: float | None, kind: str | None) -> str:
    if kind == "sell-both" or kind == "buy-both":
        return COLOR_PARITY
    if edge_bps is None:
        return COLOR_NEUTRAL
    if edge_bps >= EDGE_THRESHOLD_BPS:
        return COLOR_BUY
    if edge_bps <= -EDGE_THRESHOLD_BPS:
        return COLOR_SELL
    return COLOR_NEUTRAL


# ---------------------------------------------------------------------------
# View 1: 3D alpha surface — moneyness × time-to-expiry × edge_bps
# ---------------------------------------------------------------------------


def build_alpha_surface(snap: MarketSnapshot) -> go.Figure:
    fig = go.Figure()
    if not snap.binary_edges:
        fig.update_layout(title="No binary outcome markets available")
        return fig

    # Group strikes per expiry to draw a smooth surface; otherwise scatter.
    expiries = sorted({be.expiry_iso for be in snap.binary_edges})
    targets = sorted({be.target for be in snap.binary_edges})
    if len(expiries) >= 2 and len(targets) >= 3:
        exp_idx = {e: i for i, e in enumerate(expiries)}
        tgt_idx = {t: i for i, t in enumerate(targets)}
        Z_edge = np.full((len(expiries), len(targets)), np.nan)
        Z_iv = np.full_like(Z_edge, np.nan)
        Z_mid = np.full_like(Z_edge, np.nan)
        for be in snap.binary_edges:
            i = exp_idx[be.expiry_iso]
            j = tgt_idx[be.target]
            if be.edge_bps is not None:
                Z_edge[i, j] = be.edge_bps
            if be.iv is not None:
                Z_iv[i, j] = be.iv
            if be.yes_mid is not None:
                Z_mid[i, j] = be.yes_mid

        moneyness = np.array(targets) / max(snap.spot, 1e-9)
        hours = np.array([_hours_to_expiry(e) for e in expiries])

        fig.add_trace(go.Surface(
            x=moneyness, y=hours, z=Z_edge,
            colorscale=[
                [0.0, COLOR_SELL],
                [0.5, COLOR_NEUTRAL],
                [1.0, COLOR_BUY],
            ],
            cmid=0.0,
            colorbar=dict(title="Edge (bps)"),
            hovertemplate=(
                "Moneyness K/S=%{x:.3f}<br>"
                "TTE=%{y:.1f} h<br>"
                "Edge=%{z:.0f} bps<extra></extra>"
            ),
            opacity=0.92,
            name="alpha",
        ))

        # Highlight cells past threshold as scatter on top
        hi_x, hi_y, hi_z, hi_color, hi_text = [], [], [], [], []
        for be in snap.binary_edges:
            if be.edge_bps is None or abs(be.edge_bps) < EDGE_THRESHOLD_BPS:
                continue
            hi_x.append(be.target / max(snap.spot, 1e-9))
            hi_y.append(_hours_to_expiry(be.expiry_iso))
            hi_z.append(be.edge_bps)
            hi_color.append(COLOR_BUY if be.edge_bps > 0 else COLOR_SELL)
            hi_text.append(
                f"K={be.target:.0f}<br>YES={be.yes_mid}<br>fair={be.fair_yes:.3f}<br>"
                f"edge={be.edge_bps:.0f} bps<br>{be.arb_kind or '—'}"
            )
        if hi_x:
            fig.add_trace(go.Scatter3d(
                x=hi_x, y=hi_y, z=hi_z, mode="markers",
                marker=dict(size=7, color=hi_color, line=dict(color="#000", width=1)),
                hovertext=hi_text, hoverinfo="text", name="alpha hits",
            ))

        fig.update_layout(
            title=f"HIP-4 alpha surface — BTC spot ≈ ${snap.spot:,.0f}, σ̂={snap.sigma*100:.1f}%",
            scene=dict(
                xaxis_title="K / S (moneyness)",
                yaxis_title="Hours to expiry",
                zaxis_title="Edge (bps, fair − market)",
                aspectmode="cube",
                bgcolor="#0d1117",
            ),
            paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
            margin=dict(l=0, r=0, t=40, b=0), height=620,
        )
    else:
        # Fallback to 2D scatter
        for be in snap.binary_edges:
            color = _edge_color(be.edge_bps, be.arb_kind)
            fig.add_trace(go.Scatter(
                x=[be.target], y=[be.edge_bps or 0],
                mode="markers",
                marker=dict(size=14, color=color),
                hovertext=f"K={be.target}<br>{be.arb_kind or '—'}",
                showlegend=False,
            ))
        fig.update_layout(
            title="Edge per strike (need ≥2 expiries for surface)",
            xaxis_title="Strike", yaxis_title="Edge (bps)",
            paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            font=dict(color="#e6edf3"), height=520,
        )
    return fig


# ---------------------------------------------------------------------------
# View 2: ternary simplex (Down / Range / Up)
# ---------------------------------------------------------------------------


def build_simplex(snap: MarketSnapshot) -> go.Figure:
    fig = go.Figure()
    if not snap.ternary_edges:
        fig.update_layout(title="No ternary structures (need ≥2 strikes per expiry)")
        return fig

    market_a, market_b, market_c, market_text, market_color = [], [], [], [], []
    fair_a, fair_b, fair_c, fair_text = [], [], [], []
    for te in snap.ternary_edges:
        if te.p_down is None or te.p_range is None or te.p_up is None:
            continue
        market_a.append(te.p_down)
        market_b.append(te.p_range)
        market_c.append(te.p_up)
        market_color.append(
            COLOR_PARITY if te.arb_kind == "buy-simplex"
            else (COLOR_BUY if te.edge_range_bps and te.edge_range_bps > EDGE_THRESHOLD_BPS
                  else (COLOR_SELL if te.edge_range_bps and te.edge_range_bps < -EDGE_THRESHOLD_BPS
                        else COLOR_NEUTRAL))
        )
        edge_range_str = f"{te.edge_range_bps:.0f}" if te.edge_range_bps is not None else "—"
        market_text.append(
            f"K_low={te.k_low:.0f} K_high={te.k_high:.0f}<br>"
            f"Down={te.p_down:.3f} Range={te.p_range:.3f} Up={te.p_up:.3f}<br>"
            f"Σ={te.simplex_sum:.4f}<br>"
            f"edge_range={edge_range_str} bps<br>"
            f"arb={te.arb_kind or '—'}"
        )
        if te.fair_down is not None:
            fair_a.append(te.fair_down)
            fair_b.append(te.fair_range)
            fair_c.append(te.fair_up)
            fair_text.append(
                f"FAIR<br>K_low={te.k_low:.0f} K_high={te.k_high:.0f}<br>"
                f"Down={te.fair_down:.3f} Range={te.fair_range:.3f} Up={te.fair_up:.3f}"
            )

    fig.add_trace(go.Scatterternary(
        a=market_a, b=market_b, c=market_c, mode="markers",
        marker=dict(size=14, color=market_color, line=dict(color="#000", width=1)),
        text=market_text, hoverinfo="text", name="market",
    ))
    if fair_a:
        fig.add_trace(go.Scatterternary(
            a=fair_a, b=fair_b, c=fair_c, mode="markers",
            marker=dict(size=10, color="#00d4ff", symbol="diamond"),
            text=fair_text, hoverinfo="text", name="fair (GBM)",
        ))
        # Connect each market point to its fair counterpart
        for am, bm, cm, af, bf, cf in zip(market_a, market_b, market_c, fair_a, fair_b, fair_c):
            fig.add_trace(go.Scatterternary(
                a=[am, af], b=[bm, bf], c=[cm, cf],
                mode="lines",
                line=dict(color="rgba(255,255,255,0.25)", width=1),
                showlegend=False, hoverinfo="skip",
            ))

    fig.update_layout(
        title="Ternary simplex — Down / Range / Up (market vs GBM-fair)",
        ternary=dict(
            sum=1,
            aaxis=dict(title="P(Down)", color="#e64545"),
            baxis=dict(title="P(Range)", color="#ffc939"),
            caxis=dict(title="P(Up)", color="#1ec27a"),
            bgcolor="#0d1117",
        ),
        paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
        height=520, margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


# ---------------------------------------------------------------------------
# View 3: per-expiry RND / butterfly density
# ---------------------------------------------------------------------------


def build_density(snap: MarketSnapshot) -> go.Figure:
    fig = go.Figure()
    if not snap.densities:
        fig.update_layout(title="No multi-strike strip — density skipped")
        return fig

    for key, dens in snap.densities.items():
        bin_x = dens.strikes / max(snap.spot, 1e-9)
        colors = [COLOR_DENSITY_NEG if i in dens.pdf_negative_idx else "#7aa7ff"
                  for i in range(len(bin_x))]
        fig.add_trace(go.Bar(
            x=bin_x, y=dens.pdf, name=key,
            marker_color=colors,
            hovertemplate="K/S=%{x:.3f}<br>pdf=%{y:.3e}<extra></extra>",
        ))
        # CDF lives at the original strike grid (length N)
        n_strikes = len(dens.cdf)
        # reconstruct strike grid from midpoints + first/last extrapolation
        if n_strikes >= 2 and len(dens.strikes) >= 1:
            mids = dens.strikes
            # recover originals: original_i = mid_i - (mid_{i+1}-mid_i)/2 etc — easier: rebuild
            strikes = np.empty(n_strikes)
            strikes[1:-1] = 0.5 * (mids[1:] + mids[:-1]) if len(mids) >= 2 else mids
            strikes[0] = 2 * mids[0] - strikes[1] if len(mids) >= 2 else mids[0]
            strikes[-1] = 2 * mids[-1] - strikes[-2] if len(mids) >= 2 else mids[-1]
            cdf_x = strikes / max(snap.spot, 1e-9)
        else:
            cdf_x = bin_x
        fig.add_trace(go.Scatter(
            x=cdf_x, y=dens.cdf, mode="lines+markers", name=f"CDF · {key}",
            line=dict(color="#ffffff", dash="dot"), yaxis="y2",
        ))

    fig.update_layout(
        title="Risk-neutral density from binary strips (pink = butterfly arb)",
        barmode="group",
        xaxis=dict(title="K / S", color="#e6edf3"),
        yaxis=dict(title="pdf (1/$)", color="#e6edf3"),
        yaxis2=dict(title="CDF", overlaying="y", side="right", color="#e6edf3", range=[0, 1]),
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#e6edf3"), height=460,
    )
    return fig


# ---------------------------------------------------------------------------
# Tabular hit-list of arb opportunities
# ---------------------------------------------------------------------------


def build_opportunities(snap: MarketSnapshot) -> list[dict]:
    rows: list[dict] = []
    for be in snap.binary_edges:
        if be.arb_kind in ("sell-both", "buy-both"):
            rows.append({
                "kind": "PARITY",
                "expiry": be.expiry_iso[:16],
                "strike": be.target,
                "fair": f"{be.fair_yes:.3f}" if be.fair_yes is not None else "—",
                "market": f"YES={be.yes_mid}",
                "edge_bps": int(be.parity_violation * 10_000) if be.parity_violation else 0,
                "action": be.arb_kind.upper(),
            })
        elif be.arb_kind:
            rows.append({
                "kind": "ALPHA",
                "expiry": be.expiry_iso[:16],
                "strike": be.target,
                "fair": f"{be.fair_yes:.3f}" if be.fair_yes is not None else "—",
                "market": f"YES={be.yes_mid}",
                "edge_bps": int(be.edge_bps) if be.edge_bps is not None else 0,
                "action": be.arb_kind.upper(),
            })
    for te in snap.ternary_edges:
        if te.arb_kind == "buy-simplex":
            rows.append({
                "kind": "SIMPLEX",
                "expiry": te.expiry_iso[:16],
                "strike": f"{te.k_low:.0f}/{te.k_high:.0f}",
                "fair": f"{te.fair_down:.2f}/{te.fair_range:.2f}/{te.fair_up:.2f}"
                        if te.fair_down is not None else "—",
                "market": f"{te.p_down:.2f}/{te.p_range:.2f}/{te.p_up:.2f}"
                        if te.p_down is not None else "—",
                "edge_bps": int((1 - (te.simplex_sum or 1.0)) * 10_000),
                "action": "BUY-SIMPLEX",
            })

    rows.sort(key=lambda r: -abs(r["edge_bps"]))
    return rows
