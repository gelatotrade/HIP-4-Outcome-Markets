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
use executor::client::{extract_oids, HlClient};
use executor::config::Config;
use executor::ipc::{spawn_control_http, spawn_signal_listener, Signal, Switches};
use executor::nonce::NonceStore;
use executor::reconcile::{spawn_flatten_watcher, spawn_reconcile_loop};
use executor::risk::{check, ProposedLeg};
use executor::state::{Book, LegState};
use tokio::sync::mpsc;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

const BTC_PERP_COIN: &str = "BTC";

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
    let nonces = Arc::new(
        NonceStore::open(format!("{}/nonce.bin", cfg.state_dir))
            .context("opening persistent nonce store")?,
    );
    info!(starting_nonce = nonces.current(), "nonce store ready");

    spawn_control_http(
        cfg.control_host.clone(),
        cfg.control_port,
        book.clone(),
        switches.clone(),
    )
    .await?;

    if !dry_run {
        spawn_reconcile_loop(
            client.clone(),
            book.clone(),
            cfg.reconcile_interval_secs,
            BTC_PERP_COIN.to_string(),
        );
        spawn_flatten_watcher(
            client.clone(),
            book.clone(),
            nonces.clone(),
            switches.clone(),
            0,
            BTC_PERP_COIN.to_string(),
        );
    }

    let (tx, rx) = mpsc::channel::<Signal>(1024);
    spawn_signal_listener(cfg.signal_socket.clone(), tx).await?;

    run_loop(cfg, client, book, switches, nonces, rx, dry_run).await
}

async fn run_loop(
    cfg: Config,
    client: Arc<HlClient>,
    book: Arc<Book>,
    switches: Switches,
    nonces: Arc<NonceStore>,
    mut rx: mpsc::Receiver<Signal>,
    dry_run: bool,
) -> Result<()> {
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut sigint = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;

    loop {
        tokio::select! {
            maybe_sig = rx.recv() => {
                let Some(sig) = maybe_sig else { break };
                if let Err(e) = handle_signal(
                    &cfg, &client, &book, &switches, &nonces, sig, dry_run,
                ).await {
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
    nonces: &NonceStore,
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

    // Hedge order — opposite-side perp priced at `perp_ref_px ± slippage`.
    // BTC perp tick on Hyperliquid is $1, so we round to an integer string.
    // Refuse if the engine didn't supply a reference mid (defensive — old
    // signals from a stale producer must not silently get a $0 IOC).
    if sig.perp_ref_px <= 0.0 {
        warn!(
            id = %sig.id,
            "missing perp_ref_px on signal — refusing to submit zero-price hedge"
        );
        book.record_rejected("signal missing perp_ref_px");
        return Ok(());
    }
    let bps = if sig.slippage_bps == 0 {
        50
    } else {
        sig.slippage_bps
    };
    let slip = (bps as f64) / 10_000.0;
    let is_buy_hedge = sig.perp_delta_btc > 0.0;
    let hedge_px = if is_buy_hedge {
        sig.perp_ref_px * (1.0 + slip)
    } else {
        sig.perp_ref_px * (1.0 - slip)
    };
    let hedge_order = OrderRequest {
        a: sig.perp_asset,
        b: is_buy_hedge,
        p: format!("{}", hedge_px.round() as i64),
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

    let nonce = nonces.next()?;
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
                oids: Vec::new(),
            });
            book.add_perp_btc(sig.perp_delta_btc);

            // Attach the resting/filled order ids HL just gave us so we
            // can later cancel or reconcile against them.
            let oids = extract_oids(&resp.raw);
            let outcome_oid = oids.first().copied().flatten();
            let hedge_oid = oids.get(1).copied().flatten();
            if let Some(o) = outcome_oid {
                book.attach_oids(sig.outcome_asset, &[o]);
            }
            info!(
                id = %sig.id, status = %resp.status,
                outcome_oid = ?outcome_oid, hedge_oid = ?hedge_oid,
                "submitted"
            );
        }
        Err(e) => {
            error!(id = %sig.id, err = %e, "submit failed");
            book.record_rejected(format!("{e}"));
        }
    }
    Ok(())
}
