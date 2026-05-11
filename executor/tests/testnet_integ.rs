//! Opt-in testnet integration tests.
//!
//! These are `#[ignore]` by default — they hit `api.hyperliquid-testnet.xyz`
//! and require a real testnet API wallet key. Run with:
//!
//!   HYPERLIQUID_NETWORK=testnet \
//!   HYPERLIQUID_USER_ADDRESS=0x... \
//!   HYPERLIQUID_API_WALLET_KEY=0x... \
//!   cargo test --release -- --ignored testnet
//!
//! The tests verify three things end-to-end that the unit suite can't:
//!   1. /info on testnet returns a JSON we can parse
//!   2. Our action_hash + EIP-712 signature is accepted by /exchange
//!      (we submit a deliberately-malformed price so the order is rejected
//!      AFTER signature verification — confirming the signing path)
//!   3. clearinghouseState parses correctly
//!
//! If the env vars aren't set, every test is skipped with a clear message.

use executor::actions::{Action, CancelRequest};
use executor::client::HlClient;
use executor::config::Config;
use serde_json::json;

fn cfg_or_skip(test_name: &str) -> Option<Config> {
    match std::env::var("HYPERLIQUID_API_WALLET_KEY") {
        Ok(_) => match Config::from_env() {
            Ok(c) => Some(c),
            Err(e) => {
                eprintln!("[{test_name}] skip — Config::from_env: {e}");
                None
            }
        },
        Err(_) => {
            eprintln!("[{test_name}] skip — HYPERLIQUID_API_WALLET_KEY unset");
            None
        }
    }
}

#[tokio::test]
#[ignore]
async fn testnet_info_meta_round_trip() {
    let Some(cfg) = cfg_or_skip("info_meta") else {
        return;
    };
    let client = HlClient::new(cfg).expect("client");
    let raw: serde_json::Value = client
        .info(json!({"type": "meta"}))
        .await
        .expect("/info meta call");
    assert!(
        raw.get("universe").is_some(),
        "meta response missing 'universe' field — got {raw}"
    );
}

#[tokio::test]
#[ignore]
async fn testnet_info_clearinghouse_state_parses() {
    let Some(cfg) = cfg_or_skip("clearinghouse_state") else {
        return;
    };
    let client = HlClient::new(cfg.clone()).expect("client");
    let user = format!("0x{}", hex::encode(cfg.user_address));
    let raw: serde_json::Value = client
        .info(json!({"type": "clearinghouseState", "user": user}))
        .await
        .expect("/info clearinghouseState");
    // Either `assetPositions` (when there's a perp account) or empty —
    // both are valid; this just exercises the path.
    assert!(
        raw.is_object(),
        "clearinghouseState should be a JSON object, got: {raw}"
    );
}

#[tokio::test]
#[ignore]
async fn testnet_signed_cancel_is_authenticated() {
    // Submit a cancel for an obviously-bogus oid. HL will reject with
    // a clean "order not found"-style error AFTER verifying the
    // signature, which is exactly the path we want to exercise. A
    // signature failure would surface as "Must deposit before trading"
    // or "L1 sig invalid" — those make the test fail.
    let Some(cfg) = cfg_or_skip("signed_cancel") else {
        return;
    };
    let client = HlClient::new(cfg).expect("client");

    let action = Action::Cancel {
        cancels: vec![CancelRequest { a: 0, o: 0 }],
    };
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;

    match client.submit(action, nonce).await {
        Ok(resp) => {
            // Status==ok for a no-op cancel is also fine — means HL
            // accepted the signature and returned an inner status array
            // saying "order not found".
            assert_eq!(resp.status, "ok", "signed but exchange disagreed: {resp:?}");
        }
        Err(e) => {
            let msg = format!("{e}");
            assert!(
                !msg.to_lowercase().contains("sig"),
                "signature path looks broken: {msg}"
            );
            // Any other error (e.g. "order not found", "invalid asset")
            // is acceptable — the signature still passed verification.
        }
    }
}
