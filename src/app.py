"""Dash entrypoint for the animated HIP-4 alpha surface.

Run live (uses HYPERLIQUID_USER_ADDRESS / HYPERLIQUID_API_WALLET_KEY if set):
    python -m src.app

Run on captured CSVs (no network):
    python -m src.app --csv data/

Run fully offline (synthetic drifting universe):
    python -m src.app --no-live
"""

from __future__ import annotations

import argparse
from datetime import datetime

import dash
from dash import Input, Output, State, dash_table, dcc, html
from flask import jsonify

from .config import CONFIG
from .feed import Feed
from .logging_config import get as get_logger
from .logging_config import setup as setup_logging
from .surface import build_alpha_pnl, build_alpha_surface, build_opportunities

setup_logging(CONFIG.log_level)
log = get_logger("dashboard")


def make_app(*, allow_live: bool, csv_path: str | None, refresh_ms: int) -> dash.Dash:
    feed = Feed(allow_live=allow_live, csv_path=csv_path)
    app = dash.Dash(__name__, title="HIP-4 Animated Alpha Surface")

    slider_style = {"marginBottom": "16px"}

    app.layout = html.Div(
        style={"backgroundColor": "#0d1117", "color": "#e6edf3",
               "minHeight": "100vh", "padding": "16px",
               "fontFamily": "ui-sans-serif, system-ui, sans-serif"},
        children=[
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "alignItems": "baseline", "marginBottom": "8px"},
                children=[
                    html.H2("HIP-4 Animated Alpha Surface — IV vs RV stat-arb",
                            style={"margin": 0}),
                    html.Div(id="status", style={"opacity": 0.85, "fontFamily": "ui-monospace"}),
                ],
            ),
            html.Div(
                style={"display": "grid", "gridTemplateColumns": "3fr 1fr",
                       "gap": "12px", "alignItems": "start"},
                children=[
                    dcc.Graph(id="surface-3d", config={"displaylogo": False}),
                    html.Div(
                        style={"backgroundColor": "#161b22", "padding": "14px",
                               "border": "1px solid #30363d", "borderRadius": "8px"},
                        children=[
                            html.H4("Variables", style={"marginTop": 0}),
                            html.Label("RV window (minutes)"),
                            dcc.Slider(
                                id="vol-window", min=5, max=180, step=5, value=30,
                                marks={5: "5", 30: "30", 60: "60", 120: "120", 180: "180"},
                                tooltip={"always_visible": False},
                            ),
                            html.Div(style=slider_style),
                            html.Label("IV−RV threshold (vol pts)"),
                            dcc.Slider(
                                id="threshold", min=0.005, max=0.30, step=0.005, value=0.05,
                                marks={0.01: "1%", 0.05: "5%", 0.10: "10%", 0.20: "20%"},
                            ),
                            html.Div(style=slider_style),
                            html.Label("Hedge ratio"),
                            dcc.Slider(
                                id="hedge", min=0.0, max=2.0, step=0.05, value=1.0,
                                marks={0: "0", 0.5: "0.5", 1: "1.0", 1.5: "1.5", 2: "2.0"},
                            ),
                            html.Div(style={"marginTop": "20px"}),
                            html.Div(id="legend", style={"fontSize": "12px", "lineHeight": "1.6"}, children=[
                                html.Div(["■ ", html.Span("green", style={"color": "#1ec27a"}),
                                          " — IV < RV   buy outcome, short BTC delta"]),
                                html.Div(["■ ", html.Span("red", style={"color": "#e64545"}),
                                          " — IV > RV   sell outcome, long BTC delta"]),
                                html.Div(["■ ", html.Span("white plane", style={"color": "#bbb"}),
                                          " — RV reference"]),
                            ]),
                        ],
                    ),
                ],
            ),
            html.Div(style={"marginTop": "12px"},
                     children=[dcc.Graph(id="pnl", config={"displaylogo": False})]),
            html.Div(
                style={"marginTop": "12px"},
                children=[
                    html.H4("Active stat-arb legs (sorted by |IV − RV|)"),
                    dash_table.DataTable(
                        id="opps",
                        columns=[
                            {"name": "Expiry (UTC)", "id": "expiry"},
                            {"name": "K", "id": "K"},
                            {"name": "IV", "id": "IV"},
                            {"name": "RV", "id": "RV"},
                            {"name": "Δvol pts", "id": "Δvol_pts"},
                            {"name": "Side", "id": "side"},
                            {"name": "Perp hedge", "id": "perp_hedge"},
                            {"name": "$/day", "id": "$/day"},
                        ],
                        style_cell={
                            "backgroundColor": "#161b22", "color": "#e6edf3",
                            "border": "1px solid #30363d", "padding": "6px",
                            "fontFamily": "ui-monospace, SFMono-Regular, monospace",
                        },
                        style_header={"backgroundColor": "#21262d", "fontWeight": "bold"},
                        style_data_conditional=[
                            {"if": {"filter_query": "{side} = 'LONG-YES'"},
                             "backgroundColor": "rgba(30,194,122,0.18)"},
                            {"if": {"filter_query": "{side} = 'SHORT-YES'"},
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
        Output("pnl", "figure"),
        Output("opps", "data"),
        Output("status", "children"),
        Input("tick", "n_intervals"),
        Input("vol-window", "value"),
        Input("threshold", "value"),
        Input("hedge", "value"),
    )
    def _refresh(_n, vol_window_min, threshold, hedge):
        feed.set_params(
            vol_window_s=float(vol_window_min) * 60.0,
            threshold_vol=float(threshold),
            hedge_ratio=float(hedge),
        )
        snap = feed.snapshot()
        history = list(feed.history)

        wallet = "✓ user" if (feed._client and feed._client.user_address) else "—"
        key = "✓ key" if (feed._client and feed._client.has_wallet_key) else "—"
        status = (
            f"src={snap.source} {wallet} {key} · "
            f"BTC=${snap.spot:,.0f} · σ̂_RV={snap.sigma*100:.1f}% · "
            f"legs_active={snap.statarb.n_active} · "
            f"$/day={snap.statarb.expected_pnl_day_total:+.2f} · "
            f"hist={len(history)} · "
            f"{datetime.utcfromtimestamp(snap.ts).strftime('%H:%M:%S')}Z"
        )
        if snap.error:
            status += f" · note: {snap.error}"
        return (
            build_alpha_surface(history, threshold=float(threshold)),
            build_alpha_pnl(history),
            build_opportunities(snap),
            status,
        )

    # ------ Health endpoint for Docker / k8s liveness probes ------
    @app.server.route("/healthz")
    def _healthz():                                                 # type: ignore[no-redef]
        last = feed.history[-1] if feed.history else None
        return jsonify({
            "status": "ok",
            "source": last.source if last else "starting",
            "spot": last.spot if last else None,
            "history_len": len(feed.history),
            "active_legs": last.statarb.n_active if last else 0,
        })

    return app


# Module-level WSGI server exposed for gunicorn:
#   gunicorn src.app:server
_default_app = make_app(
    allow_live=CONFIG.user_address is not None,
    csv_path=None,
    refresh_ms=CONFIG.dashboard_refresh_ms,
)
server = _default_app.server


def main() -> None:
    parser = argparse.ArgumentParser(description="HIP-4 animated alpha surface")
    parser.add_argument("--port", type=int, default=CONFIG.dashboard_port)
    parser.add_argument("--host", default=CONFIG.dashboard_host)
    parser.add_argument("--refresh-ms", type=int, default=CONFIG.dashboard_refresh_ms)
    parser.add_argument("--no-live", action="store_true",
                        help="Skip the Hyperliquid API; use the simulator")
    parser.add_argument("--csv", default=None,
                        help="Replay CSVs captured by scripts/fetch_hl.py")
    args = parser.parse_args()
    app = make_app(
        allow_live=not args.no_live and args.csv is None,
        csv_path=args.csv,
        refresh_ms=args.refresh_ms,
    )
    log.info("dashboard.start", host=args.host, port=args.port,
             source="csv" if args.csv else ("sim" if args.no_live else "live"))
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
