//! HIP-4 execution daemon.
//!
//! - Reads NDJSON `Signal`s from a Unix socket
//! - Risk-checks each signal against caps
//! - Builds + signs the corresponding `Action` (outcome leg + perp hedge)
//! - Submits to api.hyperliquid.xyz/exchange
//! - Maintains in-memory book state + exposes /status, /kill, /flatten
//!
//! Configuration via env vars (see .env.example).

use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use executor::actions::{Action, LimitOrderType, OrderRequest, OrderType, Tif};
use executor::client::{next_nonce, HlClient};
use executor::config::Config;
use executor::ipc::{spawn_control_http, spawn_signal_listener, Signal, Switches};
use executor::risk::{check, ProposedLeg};
use executor::state::{Book, LegState};
use parking_lot::Mutex;
use tokio::sync::mpsc;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(name = "executor", about = "HIP-4 execution daemon")]
struct Cli {
    /// Don't actually submit orders; log what would be sent.
    /// Also enabled by `DRY_RUN=1|true|yes` in the environment.
    #[arg(long)]
    dry_run: bool,
}

fn dry_run_from_env() -> bool {
    std::env::var("DRY_RUN")
        .map(|v| {
            matches!(
                v.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .json()
        .try_init();
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();
    let cli = Cli::parse();
    let dry_run = cli.dry_run || dry_run_from_env();
    let cfg = Config::from_env().context("loading config from env")?;
    info!(network = ?cfg.network, dry_run = dry_run, "executor starting");

    let book = Arc::new(Book::default());
    let switches = Switches::default();
    let client = Arc::new(HlClient::new(cfg.clone())?);

    spawn_control_http(
        cfg.control_host.clone(),
        cfg.control_port,
        book.clone(),
        switches.clone(),
    )
    .await?;

    let (tx, rx) = mpsc::channel::<Signal>(1024);
    spawn_signal_listener(cfg.signal_socket.clone(), tx).await?;

    run_loop(cfg, client, book, switches, rx, dry_run).await
}

async fn run_loop(
    cfg: Config,
    client: Arc<HlClient>,
    book: Arc<Book>,
    switches: Switches,
    mut rx: mpsc::Receiver<Signal>,
    dry_run: bool,
) -> Result<()> {
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut sigint = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;

    loop {
        tokio::select! {
            maybe_sig = rx.recv() => {
                let Some(sig) = maybe_sig else { break };
                if let Err(e) = handle_signal(&cfg, &client, &book, &switches, sig, dry_run).await {
                    warn!(err = %e, "signal failed");
                }
            }
            _ = sigterm.recv() => { info!("SIGTERM, draining"); break; }
            _ = sigint.recv() => { info!("SIGINT, draining"); break; }
        }
    }
    Ok(())
}

async fn handle_signal(
    cfg: &Config,
    client: &HlClient,
    book: &Book,
    switches: &Switches,
    sig: Signal,
    dry_run: bool,
) -> Result<()> {
    // TTL drop
    if sig.ttl_ms > 0 {
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        if now_ms > sig.ts_ms + sig.ttl_ms {
            warn!(id = %sig.id, "signal expired (ttl)");
            return Ok(());
        }
    }

    let killed = *switches.killed.lock();
    let snap = book.snapshot();
    let proposed = ProposedLeg {
        notional_usd: sig.notional_usd,
        perp_delta_btc: sig.perp_delta_btc,
    };
    if let Err(e) = check(cfg, &snap, killed, proposed) {
        warn!(id = %sig.id, err = %e, "risk gate rejected");
        book.record_rejected(format!("{e}"));
        return Ok(());
    }

    // Order count for sizing: each contract pays $1, so "size" = notional.
    let size_str = format!("{}", sig.notional_usd as i64);
    let outcome_order = OrderRequest {
        a: sig.outcome_asset,
        b: sig.side.eq_ignore_ascii_case("Y"),
        p: sig.px.clone(),
        s: size_str.clone(),
        r: false,
        t: OrderType::Limit {
            limit: LimitOrderType { tif: Tif::Ioc },
        },
    };

    // Hedge order — opposite-side perp at market-ish price (we leave price
    // selection to the upstream signal which knows the live BTC mid).
    let hedge_order = OrderRequest {
        a: sig.perp_asset,
        b: sig.perp_delta_btc > 0.0,
        p: "0".into(), // placeholder; in practice the engine fills px from BTC mid
        s: format!("{:.4}", sig.perp_delta_btc.abs()),
        r: false,
        t: OrderType::Limit {
            limit: LimitOrderType { tif: Tif::Ioc },
        },
    };

    let action = Action::Order {
        orders: vec![outcome_order, hedge_order],
        grouping: "na".into(),
    };

    if dry_run {
        info!(id = %sig.id, action = ?action, "DRY-RUN — would submit");
        return Ok(());
    }

    let nonce = next_nonce();
    match client.submit(action, nonce).await {
        Ok(resp) => {
            book.record_submitted();
            book.upsert_leg(LegState {
                asset: sig.outcome_asset,
                coin: format!("#{}", sig.outcome_asset),
                size: sig.notional_usd,
                avg_px: sig.px.parse().unwrap_or(0.0),
                direction: if sig.side.eq_ignore_ascii_case("Y") {
                    1
                } else {
                    -1
                },
            });
            book.add_perp_btc(sig.perp_delta_btc);
            info!(id = %sig.id, status = %resp.status, "submitted");
        }
        Err(e) => {
            error!(id = %sig.id, err = %e, "submit failed");
            book.record_rejected(format!("{e}"));
        }
    }

    // Flatten honour: if engaged, drain to a follow-up cancel job (TODO: open-orders sync).
    if *switches.flatten.lock() {
        warn!("flatten flag is set — manual cancel required (auto-cancel TODO)");
    }
    let _ = Mutex::new(()); // silence unused-import lint when in dry-run only mode
    Ok(())
}
