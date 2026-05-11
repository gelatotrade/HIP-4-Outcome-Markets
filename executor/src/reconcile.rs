//! Reconciliation loop and the real `/flatten` action.
//!
//! The executor's in-memory Book is *advisory* — it tracks what we
//! submitted but cannot detect partial fills, cancellations done in the
//! UI, expired contracts, or any external state mutation. Without
//! reconciliation, `perp_btc` and `gross_notional` drift from reality
//! within minutes and the risk-gates make decisions on stale numbers.
//!
//! Every `cfg.reconcile_interval_secs` we pull `clearinghouseState` from
//! `/info` and `replace_all` the Book with what HL says is true.
//!
//! `/flatten` (toggled via the HTTP control plane) drains the book:
//!   1. fetch `openOrders` for the wallet
//!   2. emit a single cancel action covering every (asset, oid) pair
//!   3. fetch `clearinghouseState` and submit reduce-only IOC orders
//!      at a wide hedge price to close any residual perp exposure

use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Result};
use serde::Deserialize;
use tokio::time;
use tracing::{debug, error, info, warn};

use crate::actions::{Action, CancelRequest, LimitOrderType, OrderRequest, OrderType, Tif};
use crate::client::HlClient;
use crate::ipc::Switches;
use crate::nonce::NonceStore;
use crate::state::{Book, LegState};

#[derive(Debug, Deserialize)]
struct AssetPosition {
    #[serde(rename = "type")]
    kind: String,
    position: Position,
}

#[derive(Debug, Deserialize)]
struct Position {
    coin: String,
    szi: String, // signed size (string)
    #[serde(default, rename = "entryPx")]
    entry_px: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ClearinghouseState {
    #[serde(default, rename = "assetPositions")]
    asset_positions: Vec<AssetPosition>,
}

#[derive(Debug, Deserialize)]
struct OpenOrder {
    coin: String,
    oid: u64,
    #[serde(default, rename = "side")]
    _side: Option<String>,
    #[serde(default, rename = "sz")]
    _sz: Option<String>,
}

/// One reconciliation tick: replace the Book with HL's view.
pub async fn reconcile_once(
    client: &HlClient,
    book: &Book,
    perp_asset_for_coin: impl Fn(&str) -> Option<u32>,
    btc_perp_coin: &str,
) -> Result<()> {
    let user = format!("0x{}", hex::encode(client.cfg().user_address));
    let payload = serde_json::json!({"type": "clearinghouseState", "user": user});
    let state: ClearinghouseState = client
        .info(payload)
        .await
        .map_err(|e| anyhow!("clearinghouseState: {e}"))?;

    let mut legs: Vec<LegState> = Vec::new();
    let mut perp_btc = 0.0;
    for ap in &state.asset_positions {
        let szi: f64 = ap.position.szi.parse().unwrap_or(0.0);
        if szi.abs() < 1e-9 {
            continue;
        }
        if ap.position.coin == btc_perp_coin {
            perp_btc = szi;
            continue;
        }
        let asset = match perp_asset_for_coin(&ap.position.coin) {
            Some(a) => a,
            None => continue,
        };
        let direction: i8 = if szi > 0.0 { 1 } else { -1 };
        let avg_px = ap
            .position
            .entry_px
            .as_deref()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0);
        legs.push(LegState {
            asset,
            coin: ap.position.coin.clone(),
            size: szi.abs(),
            avg_px,
            direction,
            oids: Vec::new(),
        });
        let _ = ap.kind.as_str();
    }
    let n = legs.len();
    book.replace_all(legs, perp_btc);
    debug!(legs = n, perp_btc, "reconciled book from /info");
    Ok(())
}

/// Spawn the reconciliation loop. Returns immediately; the loop runs
/// until the process is killed.
pub fn spawn_reconcile_loop(
    client: Arc<HlClient>,
    book: Arc<Book>,
    interval_secs: u64,
    btc_perp_coin: String,
) {
    tokio::spawn(async move {
        let mut tick = time::interval(Duration::from_secs(interval_secs.max(1)));
        // The function-pointer can't capture context, so we use a tiny
        // closure that ignores coin and looks at a fixed mapping. The
        // engine populates outcome asset ids at signal time anyway, so
        // unknown coins are simply skipped during reconciliation.
        let map_coin = |coin: &str| -> Option<u32> {
            coin.strip_prefix('#').and_then(|s| s.parse::<u32>().ok())
        };
        loop {
            tick.tick().await;
            if let Err(e) = reconcile_once(&client, &book, &map_coin, &btc_perp_coin).await {
                warn!(err = %e, "reconcile tick failed");
            }
        }
    });
}

/// Drain every open order + perp exposure for the configured wallet.
pub async fn flatten(
    client: &HlClient,
    book: &Book,
    nonces: &NonceStore,
    btc_perp_asset: u32,
    btc_perp_coin: &str,
) -> Result<()> {
    info!("flatten: fetching open orders");
    let user = format!("0x{}", hex::encode(client.cfg().user_address));
    let openorders_payload = serde_json::json!({"type": "openOrders", "user": user});
    let open: Vec<OpenOrder> = client.info(openorders_payload).await.unwrap_or_default();

    let cancels: Vec<CancelRequest> = open
        .iter()
        .filter_map(|o| {
            let asset = o
                .coin
                .strip_prefix('#')
                .and_then(|s| s.parse::<u32>().ok())?;
            Some(CancelRequest { a: asset, o: o.oid })
        })
        .collect();

    if !cancels.is_empty() {
        info!(n = cancels.len(), "flatten: cancelling open orders");
        let action = Action::Cancel { cancels };
        let nonce = nonces.next()?;
        if let Err(e) = client.submit(action, nonce).await {
            error!(err = %e, "flatten: cancel submit failed");
        }
    }

    info!("flatten: pulling clearinghouseState for residual closes");
    let state_payload = serde_json::json!({"type": "clearinghouseState", "user": user});
    let state: ClearinghouseState = client
        .info(state_payload)
        .await
        .map_err(|e| anyhow!("clearinghouseState: {e}"))?;

    let mut closes: Vec<OrderRequest> = Vec::new();
    for ap in &state.asset_positions {
        let szi: f64 = ap.position.szi.parse().unwrap_or(0.0);
        if szi.abs() < 1e-9 {
            continue;
        }
        let (asset, px_str) = if ap.position.coin == btc_perp_coin {
            // BTC perp — close at the mid (engine has no ref here, so we
            // submit a wide IOC by picking the entry-price ± 5%).
            let entry: f64 = ap
                .position
                .entry_px
                .as_deref()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0.0);
            let px = if szi > 0.0 {
                entry * 0.95
            } else {
                entry * 1.05
            };
            (btc_perp_asset, format!("{}", px.round() as i64))
        } else {
            let asset = match ap
                .position
                .coin
                .strip_prefix('#')
                .and_then(|s| s.parse::<u32>().ok())
            {
                Some(a) => a,
                None => continue,
            };
            // Wide outcome close at probability 0.5 (IOC will only fill
            // against the touching side anyway). Reduce-only protects
            // against accidentally opening the opposite leg.
            (asset, "0.5".to_string())
        };
        closes.push(OrderRequest {
            a: asset,
            b: szi < 0.0, // long position ⇒ sell, short ⇒ buy
            p: px_str,
            s: format!("{:.4}", szi.abs()),
            r: true, // reduce-only
            t: OrderType::Limit {
                limit: LimitOrderType { tif: Tif::Ioc },
            },
        });
    }

    if !closes.is_empty() {
        info!(n = closes.len(), "flatten: closing residual positions");
        let action = Action::Order {
            orders: closes,
            grouping: "na".into(),
        };
        let nonce = nonces.next()?;
        if let Err(e) = client.submit(action, nonce).await {
            error!(err = %e, "flatten: close submit failed");
        }
    }

    book.replace_all(Vec::new(), 0.0);
    Ok(())
}

/// Spawn a watcher that fires `flatten()` whenever the flag is engaged
/// via the HTTP control plane. Re-arms after each completion.
pub fn spawn_flatten_watcher(
    client: Arc<HlClient>,
    book: Arc<Book>,
    nonces: Arc<NonceStore>,
    switches: Switches,
    btc_perp_asset: u32,
    btc_perp_coin: String,
) {
    tokio::spawn(async move {
        loop {
            // Cheap poll — only does work when the flag is set.
            tokio::time::sleep(Duration::from_millis(250)).await;
            let triggered = {
                let mut g = switches.flatten.lock();
                if *g {
                    *g = false;
                    true
                } else {
                    false
                }
            };
            if triggered {
                warn!("flatten: executing now");
                if let Err(e) =
                    flatten(&client, &book, &nonces, btc_perp_asset, &btc_perp_coin).await
                {
                    error!(err = %e, "flatten: aborted");
                } else {
                    info!("flatten: done");
                    // Keep the kill-switch engaged so we don't immediately
                    // re-open positions on the next signal.
                    *switches.killed.lock() = true;
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Book;

    #[test]
    fn replace_all_overwrites_book() {
        let book = Book::default();
        book.upsert_leg(LegState {
            asset: 1,
            coin: "#1".into(),
            size: 10.0,
            avg_px: 0.4,
            direction: 1,
            oids: vec![],
        });
        assert_eq!(book.snapshot().n_legs, 1);

        book.replace_all(
            vec![
                LegState {
                    asset: 2,
                    coin: "#2".into(),
                    size: 5.0,
                    avg_px: 0.3,
                    direction: -1,
                    oids: vec![],
                },
                LegState {
                    asset: 3,
                    coin: "#3".into(),
                    size: 1.0,
                    avg_px: 0.7,
                    direction: 1,
                    oids: vec![],
                },
            ],
            -0.5,
        );
        let snap = book.snapshot();
        assert_eq!(snap.n_legs, 2);
        assert_eq!(snap.perp_btc, -0.5);
        assert!(snap.legs.iter().all(|l| l.asset != 1));
    }
}
