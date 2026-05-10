//! IPC surface:
//!
//! 1. **Hot path** — Unix Domain Socket carrying NDJSON `Signal`
//!    messages, one per line. Single-host, ~10 µs per message.
//!    Tools: `socat - UNIX-CONNECT:/tmp/hip4-exec.sock`
//!
//! 2. **Control plane** — HTTP on `EXECUTOR_CONTROL_HOST:PORT`.
//!    Endpoints:
//!    - GET  /healthz   liveness
//!    - GET  /status    BookSnapshot JSON
//!    - POST /kill      engages kill-switch (no new orders)
//!    - POST /resume    lifts the kill-switch
//!    - POST /flatten   cancels every open leg + closes perp
//!
//! The Python signal engine writes to the UDS; humans operate via HTTP.

use std::sync::Arc;

use anyhow::Result;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::UnixListener;
use tokio::sync::mpsc;
use tracing::{error, info, warn};
use warp::Filter;

use crate::state::Book;

/// Wire-format signal coming in over the UDS.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Signal {
    /// Engine-side correlation id for tracing.
    pub id: String,
    /// "open" | "close" | "rebalance"
    pub kind: String,
    /// Asset id of the outcome leg
    pub outcome_asset: u32,
    /// `Y` or `N` side
    pub side: String,
    /// Quote price as a string (must match HL tick size)
    pub px: String,
    /// Notional in USD (= number of contracts; each pays $1)
    pub notional_usd: f64,
    /// Required perp delta hedge in BTC, signed (positive = long perp)
    pub perp_delta_btc: f64,
    /// Asset id of BTC perp (for the hedge order)
    pub perp_asset: u32,
    /// Engine timestamp (ms since epoch); the executor rejects stale signals
    pub ts_ms: u64,
    /// TTL in ms (0 = no expiry)
    #[serde(default)]
    pub ttl_ms: u64,
}

#[derive(Debug, Clone)]
pub struct Switches {
    pub killed: Arc<Mutex<bool>>,
    pub flatten: Arc<Mutex<bool>>,
}

impl Default for Switches {
    fn default() -> Self {
        Self {
            killed: Arc::new(Mutex::new(false)),
            flatten: Arc::new(Mutex::new(false)),
        }
    }
}

/// Spawn a UDS listener that pushes parsed `Signal`s into `tx`.
pub async fn spawn_signal_listener(socket_path: String, tx: mpsc::Sender<Signal>) -> Result<()> {
    let _ = std::fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path)?;
    info!(path = %socket_path, "signal UDS listening");

    tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    let tx = tx.clone();
                    tokio::spawn(async move {
                        let reader = BufReader::new(stream);
                        let mut lines = reader.lines();
                        while let Ok(Some(line)) = lines.next_line().await {
                            let line = line.trim();
                            if line.is_empty() {
                                continue;
                            }
                            match serde_json::from_str::<Signal>(line) {
                                Ok(sig) => {
                                    if let Err(e) = tx.send(sig).await {
                                        error!(err = %e, "signal channel closed");
                                        break;
                                    }
                                }
                                Err(e) => warn!(err = %e, line = line, "bad signal"),
                            }
                        }
                    });
                }
                Err(e) => {
                    error!(err = %e, "uds accept");
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                }
            }
        }
    });
    Ok(())
}

/// Spawn the HTTP control plane.
pub async fn spawn_control_http(
    host: String,
    port: u16,
    book: Arc<Book>,
    switches: Switches,
) -> Result<()> {
    let book_status = book.clone();
    let healthz = warp::path("healthz")
        .and(warp::get())
        .map(|| warp::reply::json(&serde_json::json!({"status": "ok"})));

    let status = warp::path("status")
        .and(warp::get())
        .map(move || warp::reply::json(&book_status.snapshot()));

    let kill_sw = switches.killed.clone();
    let kill = warp::path("kill").and(warp::post()).map(move || {
        *kill_sw.lock() = true;
        warn!("kill-switch ENGAGED");
        warp::reply::json(&serde_json::json!({"killed": true}))
    });

    let resume_sw = switches.killed.clone();
    let resume = warp::path("resume").and(warp::post()).map(move || {
        *resume_sw.lock() = false;
        info!("kill-switch lifted");
        warp::reply::json(&serde_json::json!({"killed": false}))
    });

    let flatten_sw = switches.flatten.clone();
    let flatten = warp::path("flatten").and(warp::post()).map(move || {
        *flatten_sw.lock() = true;
        warn!("flatten requested");
        warp::reply::json(&serde_json::json!({"flatten": true}))
    });

    let routes = healthz.or(status).or(kill).or(resume).or(flatten);
    let addr: std::net::SocketAddr = format!("{host}:{port}").parse()?;
    info!(%addr, "control HTTP listening");
    tokio::spawn(warp::serve(routes).run(addr));
    Ok(())
}
