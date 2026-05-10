"""Dash entrypoint — run `python -m src.app`."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

import dash
from dash import Input, Output, dash_table, dcc, html

from .feed import Feed
from .surface import (
    build_alpha_surface,
    build_density,
    build_opportunities,
    build_simplex,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def make_app(*, allow_live: bool, refresh_ms: int) -> dash.Dash:
    feed = Feed(allow_live=allow_live)
    app = dash.Dash(__name__, title="HIP-4 Alpha Surface")
    app.layout = html.Div(
        style={"backgroundColor": "#0d1117", "color": "#e6edf3",
               "minHeight": "100vh", "padding": "16px",
               "fontFamily": "ui-sans-serif, system-ui, sans-serif"},
        children=[
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "alignItems": "baseline", "marginBottom": "8px"},
                children=[
                    html.H2("HIP-4 Alpha & Arbitrage Surface", style={"margin": 0}),
                    html.Div(id="status", style={"opacity": 0.8}),
                ],
            ),
            html.Div(
                style={"display": "grid",
                       "gridTemplateColumns": "1.4fr 1fr",
                       "gap": "12px"},
                children=[
                    dcc.Graph(id="surface-3d", config={"displaylogo": False}),
                    dcc.Graph(id="simplex", config={"displaylogo": False}),
                ],
            ),
            html.Div(
                style={"marginTop": "12px"},
                children=[dcc.Graph(id="density", config={"displaylogo": False})],
            ),
            html.Div(
                style={"marginTop": "12px"},
                children=[
                    html.H4("Live opportunities (sorted by |edge|)"),
                    dash_table.DataTable(
                        id="opps",
                        columns=[
                            {"name": "Kind", "id": "kind"},
                            {"name": "Expiry (UTC)", "id": "expiry"},
                            {"name": "Strike", "id": "strike"},
                            {"name": "Fair", "id": "fair"},
                            {"name": "Market", "id": "market"},
                            {"name": "Edge (bps)", "id": "edge_bps"},
                            {"name": "Action", "id": "action"},
                        ],
                        style_cell={
                            "backgroundColor": "#161b22", "color": "#e6edf3",
                            "border": "1px solid #30363d", "padding": "6px",
                            "fontFamily": "ui-monospace, SFMono-Regular, monospace",
                        },
                        style_header={"backgroundColor": "#21262d", "fontWeight": "bold"},
                        style_data_conditional=[
                            {"if": {"filter_query": "{kind} = 'PARITY'"}, "color": "#ffc939"},
                            {"if": {"filter_query": "{kind} = 'SIMPLEX'"}, "color": "#ff7ad9"},
                            {"if": {"filter_query": "{action} contains 'long' || {action} contains 'BUY'"},
                             "backgroundColor": "rgba(30,194,122,0.18)"},
                            {"if": {"filter_query": "{action} contains 'short' || {action} contains 'SELL'"},
                             "backgroundColor": "rgba(230,69,69,0.18)"},
                        ],
                    ),
                ],
            ),
            dcc.Interval(id="tick", interval=refresh_ms, n_intervals=0),
        ],
    )

    @app.callback(
        Output("surface-3d", "figure"),
        Output("simplex", "figure"),
        Output("density", "figure"),
        Output("opps", "data"),
        Output("status", "children"),
        Input("tick", "n_intervals"),
    )
    def _refresh(_n: int):
        snap = feed.snapshot()
        status = (
            f"source={snap.source} · spot=${snap.spot:,.0f} · "
            f"σ̂={snap.sigma*100:.1f}% · "
            f"binaries={len(snap.binary_edges)} · "
            f"ternaries={len(snap.ternary_edges)} · "
            f"updated={datetime.utcfromtimestamp(snap.ts).strftime('%H:%M:%S')}Z"
        )
        if snap.error:
            status += f" · note: {snap.error}"
        return (
            build_alpha_surface(snap),
            build_simplex(snap),
            build_density(snap),
            build_opportunities(snap),
            status,
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HIP-4 alpha/arbitrage surface")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--refresh-ms", type=int, default=2000)
    parser.add_argument("--no-live", action="store_true",
                        help="Force simulator (no calls to api.hyperliquid.xyz)")
    args = parser.parse_args()
    app = make_app(allow_live=not args.no_live, refresh_ms=args.refresh_ms)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
